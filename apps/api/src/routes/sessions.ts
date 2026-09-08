import { randomInt } from "node:crypto";
import {
  canonicalSegments,
  consentRecords,
  participants,
  sessionEvents,
  sessions,
} from "@amphi/db";
import { CLOCK_SYNC_ROUND_TRIPS } from "@amphi/shared";
import { and, asc, desc, eq, gt, isNull } from "drizzle-orm";
import type { FastifyInstance } from "fastify";
import { z } from "zod";
import { requireAuth } from "../auth.js";
import type { AppContext } from "../context.js";

/** Fenêtre pendant laquelle un nouvel arrivant rejoint la séance en cours plutôt que d'en créer une. */
const JOIN_WINDOW_MS = 4 * 60 * 60 * 1000;

/** Sans I, O, 0, 1 : le code est lu à voix haute dans un amphi bruyant. */
const CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";

function generateJoinCode(): string {
  return Array.from({ length: 6 }, () => CODE_ALPHABET[randomInt(CODE_ALPHABET.length)]).join("");
}

const JoinBody = z.object({
  courseId: z.uuid().optional(),
  joinCode: z.string().length(6).optional(),
  deviceLabel: z.string().min(1).max(120),
  room: z.string().max(120).optional(),
});

const ClockBody = z.object({
  clockA: z.number().finite(),
  clockB: z.number().finite(),
});

const EventBody = z.object({
  kind: z.enum(["not-understood", "marked-important", "gap-detected", "stream-desynced"]),
  atSessionMs: z.number().nonnegative(),
  payload: z.record(z.string(), z.unknown()).default({}),
});

export function registerSessionRoutes(app: FastifyInstance, ctx: AppContext): void {
  const auth = { preHandler: requireAuth(ctx) };

  /**
   * Échange d'horodatages pour l'algorithme de Cristian. Le client en fait sept
   * et garde la médiane du quartile de plus faible RTT (voir clock.ts) — c'est
   * lui qui calcule, le serveur ne fait que dater honnêtement.
   */
  app.post("/api/clock/sync", async (request, reply) => {
    const serverRecvMs = Date.now();
    const body = z.object({ clientSendMs: z.number() }).safeParse(request.body);
    if (!body.success) return reply.code(400).send({ error: "clientSendMs manquant" });
    return reply.send({
      clientSendMs: body.data.clientSendMs,
      serverRecvMs,
      serverSendMs: Date.now(),
      roundTripsExpected: CLOCK_SYNC_ROUND_TRIPS,
    });
  });

  app.post("/api/sessions/join", auth, async (request, reply) => {
    const body = JoinBody.safeParse(request.body);
    if (!body.success) return reply.code(400).send({ error: body.error.issues });
    const user = request.user;
    if (user === undefined) return reply.code(401).send({ error: "authentification requise" });

    const { courseId, joinCode, deviceLabel, room } = body.data;
    if (courseId === undefined && joinCode === undefined) {
      return reply.code(400).send({ error: "courseId ou joinCode requis" });
    }

    let session = undefined;

    if (joinCode !== undefined) {
      const found = await ctx.db.select().from(sessions).where(eq(sessions.joinCode, joinCode)).limit(1);
      session = found[0];
      if (session === undefined) return reply.code(404).send({ error: "code de séance inconnu" });
    } else if (courseId !== undefined) {
      // Séance déjà ouverte pour ce cours : on rejoint plutôt que d'en créer une
      // deuxième, sinon deux étudiants du même amphi produisent deux transcriptions
      // qui ne seront jamais fusionnées.
      const found = await ctx.db
        .select()
        .from(sessions)
        .where(
          and(
            eq(sessions.courseId, courseId),
            eq(sessions.status, "recording"),
            gt(sessions.startedAt, new Date(Date.now() - JOIN_WINDOW_MS)),
          ),
        )
        .orderBy(desc(sessions.startedAt))
        .limit(1);
      session = found[0];
    }

    // Le consentement est porté par le cours (§10). Sans trace, aucun chunk n'est
    // accepté : c'est un verrou serveur, pas une case à cocher dans l'interface.
    let consentGranted = false;
    const effectiveCourseId = session?.courseId ?? courseId ?? null;
    if (effectiveCourseId !== null) {
      const consent = await ctx.db
        .select()
        .from(consentRecords)
        .where(and(eq(consentRecords.courseId, effectiveCourseId), isNull(consentRecords.revokedAt)))
        .limit(1);
      consentGranted = consent.length > 0;
    }

    if (session === undefined) {
      if (!consentGranted) {
        return reply.code(403).send({
          error: "aucun consentement enregistré pour ce cours",
          hint: "enregistre l'accord de l'enseignant avant d'ouvrir une séance",
        });
      }
      const created = await ctx.db
        .insert(sessions)
        .values({
          courseId: courseId ?? null,
          room: room ?? null,
          status: "recording",
          joinCode: generateJoinCode(),
        })
        .returning();
      session = created[0];
      if (session === undefined) return reply.code(500).send({ error: "création de séance échouée" });
    }

    const participant = await ctx.db
      .insert(participants)
      .values({
        sessionId: session.id,
        userId: user.id,
        deviceLabel,
        role: consentGranted ? "recorder" : "viewer",
      })
      .onConflictDoUpdate({
        target: [participants.sessionId, participants.userId],
        set: { deviceLabel },
      })
      .returning();

    const joined = participant[0];
    if (joined === undefined) return reply.code(500).send({ error: "adhésion à la séance échouée" });

    await ctx.db.insert(sessionEvents).values({
      sessionId: session.id,
      userId: user.id,
      kind: "joined",
      atSessionMs: Math.max(0, Date.now() - session.startedAt.getTime()),
    });

    return reply.send({
      sessionId: session.id,
      participantId: joined.id,
      joinCode: session.joinCode,
      startedAt: session.startedAt.toISOString(),
      courseId: session.courseId,
      costProfile: session.costProfile,
      consentGranted,
      canRecord: consentGranted,
    });
  });

  /** Le client renvoie son modèle d'horloge affiné après les allers-retours. */
  app.post<{ Params: { participantId: string } }>(
    "/api/participants/:participantId/clock",
    auth,
    async (request, reply) => {
      const body = ClockBody.safeParse(request.body);
      if (!body.success) return reply.code(400).send({ error: body.error.issues });
      const user = request.user;
      if (user === undefined) return reply.code(401).send({ error: "authentification requise" });

      const updated = await ctx.db
        .update(participants)
        .set({ clockA: body.data.clockA, clockB: body.data.clockB })
        .where(and(eq(participants.id, request.params.participantId), eq(participants.userId, user.id)))
        .returning({ id: participants.id });

      if (updated.length === 0) return reply.code(404).send({ error: "participant inconnu" });
      return reply.send({ ok: true });
    },
  );

  app.post<{ Params: { sessionId: string } }>(
    "/api/sessions/:sessionId/end",
    auth,
    async (request, reply) => {
      const updated = await ctx.db
        .update(sessions)
        .set({ endedAt: new Date(), status: "processing" })
        .where(and(eq(sessions.id, request.params.sessionId), eq(sessions.status, "recording")))
        .returning({ id: sessions.id, endedAt: sessions.endedAt });
      if (updated.length === 0) return reply.code(404).send({ error: "séance introuvable ou déjà close" });
      return reply.send({ ok: true, endedAt: updated[0]?.endedAt?.toISOString() });
    },
  );

  /**
   * Événement daté générique. Alimente déjà le futur bouton « je n'ai pas
   * compris » : l'agrégation des timestamps où plusieurs étudiants ont cliqué
   * est un signal collectif qu'on ne peut obtenir d'aucune autre façon.
   */
  app.post<{ Params: { sessionId: string } }>(
    "/api/sessions/:sessionId/events",
    auth,
    async (request, reply) => {
      const body = EventBody.safeParse(request.body);
      if (!body.success) return reply.code(400).send({ error: body.error.issues });
      const user = request.user;
      if (user === undefined) return reply.code(401).send({ error: "authentification requise" });

      await ctx.db.insert(sessionEvents).values({
        sessionId: request.params.sessionId,
        userId: user.id,
        kind: body.data.kind,
        atSessionMs: body.data.atSessionMs,
        payload: body.data.payload,
      });
      return reply.code(201).send({ ok: true });
    },
  );

  /**
   * Transcription canonique. Le client garde un curseur `sinceMs` et rejoue
   * depuis là : la lecture est incrémentale, y compris après une coupure réseau.
   */
  app.get<{ Params: { sessionId: string }; Querystring: { sinceMs?: string; limit?: string } }>(
    "/api/sessions/:sessionId/transcript",
    auth,
    async (request, reply) => {
      const sinceMs = Number(request.query.sinceMs ?? 0);
      const limit = Math.min(Number(request.query.limit ?? 500), 2000);
      if (!Number.isFinite(sinceMs) || sinceMs < 0) {
        return reply.code(400).send({ error: "sinceMs invalide" });
      }

      const rows = await ctx.db
        .select()
        .from(canonicalSegments)
        .where(
          and(
            eq(canonicalSegments.sessionId, request.params.sessionId),
            gt(canonicalSegments.startMs, sinceMs - 1),
          ),
        )
        .orderBy(asc(canonicalSegments.startMs))
        .limit(limit);

      const last = rows.at(-1);
      return reply.send({
        segments: rows,
        nextSinceMs: last === undefined ? sinceMs : last.startMs + 1,
        hasMore: rows.length === limit,
      });
    },
  );
}
