import Fastify, { type FastifyInstance } from "fastify";
import { z } from "zod";
import { consumeMagicLink, createMagicLink } from "./auth.js";
import type { AppContext } from "./context.js";
import { registerChunkRoutes } from "./routes/chunks.js";
import { registerSessionRoutes } from "./routes/sessions.js";

export function buildServer(ctx: AppContext): FastifyInstance {
  const app = Fastify({
    logger: {
      level: ctx.config.NODE_ENV === "production" ? "info" : "debug",
      // Les en-têtes d'authentification ne doivent jamais atterrir dans un log
      // qui finira dans une sauvegarde ou un partage d'écran.
      redact: ["req.headers.authorization", "req.headers.cookie"],
    },
    bodyLimit: 2_000_000,
  });

  app.get("/health", async () => ({ ok: true, at: new Date().toISOString() }));

  app.post("/api/auth/request-link", async (request, reply) => {
    const body = z.object({ email: z.email() }).safeParse(request.body);
    if (!body.success) return reply.code(400).send({ error: "adresse invalide" });

    const token = await createMagicLink(ctx, body.data.email.toLowerCase());
    if (token !== null && ctx.config.SMTP_URL === undefined) {
      // En développement, le lien va dans les logs : pas de SMTP à configurer
      // pour commencer à travailler.
      app.log.info({ magicLink: `/auth/verify?token=${token}` }, "lien magique émis");
    }
    // Réponse identique dans tous les cas : cet endpoint ne doit pas révéler qui
    // fait partie de la promo.
    return reply.send({ ok: true });
  });

  app.post("/api/auth/verify", async (request, reply) => {
    const body = z.object({ token: z.string().min(10) }).safeParse(request.body);
    if (!body.success) return reply.code(400).send({ error: "jeton invalide" });

    const sessionToken = await consumeMagicLink(ctx, body.data.token);
    if (sessionToken === null) return reply.code(401).send({ error: "lien expiré ou déjà utilisé" });
    return reply.send({ token: sessionToken });
  });

  registerSessionRoutes(app, ctx);
  registerChunkRoutes(app, ctx);

  return app;
}
