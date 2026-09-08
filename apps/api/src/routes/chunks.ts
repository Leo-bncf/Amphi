import { audioChunks, participants, sessions } from "@amphi/db";
import { chunkStorageKey } from "@amphi/providers";
import { and, eq } from "drizzle-orm";
import type { FastifyInstance } from "fastify";
import { z } from "zod";
import { requireAuth } from "../auth.js";
import type { AppContext } from "../context.js";

/** 30 s d'Opus à 24 kbps font ~90 ko. On laisse dix fois la marge, pas plus. */
const MAX_CHUNK_BYTES = 1_000_000;

const ChunkHeaders = z.object({
  "x-participant-id": z.uuid(),
  "x-seq": z.coerce.number().int().nonnegative(),
  "x-started-at-session-ms": z.coerce.number().int().nonnegative(),
  "x-duration-ms": z.coerce.number().int().positive(),
  "x-has-gap": z
    .string()
    .optional()
    .transform((v) => v === "1" || v === "true"),
  "x-mime-type": z.string().default("audio/ogg"),
  /** Enveloppe d'énergie 50 Hz en base64 — survit à la suppression de l'audio. */
  "x-envelope": z.string().optional(),
});

export function registerChunkRoutes(app: FastifyInstance, ctx: AppContext): void {
  // Le corps arrive en binaire brut : pas de multipart, pas de base64. Un chunk
  // sur un réseau d'amphi, chaque octet compte.
  app.addContentTypeParser(
    "application/octet-stream",
    { parseAs: "buffer", bodyLimit: MAX_CHUNK_BYTES },
    (_request, body, done) => {
      done(null, body);
    },
  );

  app.post<{ Params: { sessionId: string } }>(
    "/api/sessions/:sessionId/chunks",
    { preHandler: requireAuth(ctx) },
    async (request, reply) => {
      const headers = ChunkHeaders.safeParse(request.headers);
      if (!headers.success) return reply.code(400).send({ error: headers.error.issues });

      const body = request.body;
      if (!Buffer.isBuffer(body) || body.byteLength === 0) {
        return reply.code(400).send({ error: "corps binaire attendu" });
      }

      const user = request.user;
      if (user === undefined) return reply.code(401).send({ error: "authentification requise" });

      const { sessionId } = request.params;
      const participantId = headers.data["x-participant-id"];
      const seq = headers.data["x-seq"];

      // Le participant doit appartenir à cette séance ET à cet utilisateur : sans
      // ça, un identifiant deviné suffirait à injecter de l'audio dans le cours
      // de quelqu'un d'autre.
      const rows = await ctx.db
        .select({ role: participants.role, status: sessions.status })
        .from(participants)
        .innerJoin(sessions, eq(sessions.id, participants.sessionId))
        .where(
          and(
            eq(participants.id, participantId),
            eq(participants.sessionId, sessionId),
            eq(participants.userId, user.id),
          ),
        )
        .limit(1);

      const membership = rows[0];
      if (membership === undefined) return reply.code(403).send({ error: "participant non autorisé" });
      if (membership.role !== "recorder") {
        // Pas de consentement enregistré pour ce cours : le verrou est ici, côté
        // serveur, et pas seulement dans l'interface.
        return reply.code(403).send({ error: "enregistrement non autorisé pour cette séance" });
      }
      if (membership.status !== "recording") {
        return reply.code(409).send({ error: "la séance n'est plus en enregistrement" });
      }

      const storageKey = chunkStorageKey(sessionId, participantId, seq);

      // Idempotence : la file offline retente indéfiniment, et un chunk rejoué
      // ne doit produire ni doublon en base, ni second job de transcription.
      const inserted = await ctx.db
        .insert(audioChunks)
        .values({
          participantId,
          seq,
          startedAtSessionMs: headers.data["x-started-at-session-ms"],
          durationMs: headers.data["x-duration-ms"],
          storageKey,
          status: "uploaded",
          hasGap: headers.data["x-has-gap"] ?? false,
          envelope: headers.data["x-envelope"] ?? null,
          deleteAfter:
            ctx.config.AUDIO_RETENTION_DAYS === 0
              ? null
              : new Date(Date.now() + ctx.config.AUDIO_RETENTION_DAYS * 86_400_000),
        })
        .onConflictDoNothing({ target: [audioChunks.participantId, audioChunks.seq] })
        .returning({ id: audioChunks.id });

      const chunk = inserted[0];
      if (chunk === undefined) {
        // Déjà reçu. On répond 200 et non 409 : pour le client, l'objectif est
        // atteint, il peut retirer le chunk de sa file locale.
        return reply.send({ ok: true, duplicate: true, seq });
      }

      // L'écriture disque vient après l'insertion : si elle échoue, le job de
      // transcription échouera bruyamment plutôt que de disparaître en silence.
      await ctx.storage.put(storageKey, new Uint8Array(body));

      await ctx.transcribeQueue.add(
        "transcribe",
        {
          chunkId: chunk.id,
          sessionId,
          participantId,
          storageKey,
          mimeType: headers.data["x-mime-type"],
        },
        { jobId: chunk.id },
      );

      return reply.code(201).send({ ok: true, duplicate: false, seq, chunkId: chunk.id });
    },
  );
}
