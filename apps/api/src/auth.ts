import { randomBytes } from "node:crypto";
import { authSessions, invites, users } from "@amphi/db";
import { and, eq, gt, isNull } from "drizzle-orm";
import type { FastifyReply, FastifyRequest } from "fastify";
import type { AppContext } from "./context.js";

/**
 * Authentification par lien magique restreinte à une liste d'invités — ADR-10.
 *
 * Le brief demandait un filtre par domaine de messagerie. Une liste nominative
 * est à la fois plus simple et plus stricte pour trente personnes : un domaine
 * autoriserait aussi les trois mille autres étudiants de l'école, et il oblige
 * chacun à avoir une adresse du même établissement — or la promo est à cheval
 * sur deux écoles.
 */

const MAGIC_LINK_TTL_MS = 15 * 60 * 1000;
const SESSION_TTL_MS = 90 * 24 * 60 * 60 * 1000;

export interface AuthenticatedUser {
  readonly id: string;
  readonly email: string;
  readonly name: string;
}

declare module "fastify" {
  interface FastifyRequest {
    user?: AuthenticatedUser;
  }
}

function newToken(): string {
  return randomBytes(32).toString("base64url");
}

export async function isInvited(ctx: AppContext, email: string): Promise<boolean> {
  const rows = await ctx.db.select().from(invites).where(eq(invites.email, email)).limit(1);
  return rows.length > 0;
}

/**
 * Crée un lien magique. Renvoie toujours le même résultat qu'une adresse soit
 * invitée ou non côté appelant : on ne veut pas que l'endpoint devienne un
 * oracle qui dit qui fait partie de la promo.
 */
export async function createMagicLink(ctx: AppContext, email: string): Promise<string | null> {
  if (!(await isInvited(ctx, email))) return null;
  const token = newToken();
  const { magicLinks } = await import("@amphi/db");
  await ctx.db.insert(magicLinks).values({
    token,
    email,
    expiresAt: new Date(Date.now() + MAGIC_LINK_TTL_MS),
  });
  return token;
}

export async function consumeMagicLink(ctx: AppContext, token: string): Promise<string | null> {
  const { magicLinks } = await import("@amphi/db");
  const now = new Date();

  const rows = await ctx.db
    .select()
    .from(magicLinks)
    .where(and(eq(magicLinks.token, token), isNull(magicLinks.consumedAt), gt(magicLinks.expiresAt, now)))
    .limit(1);

  const link = rows[0];
  if (link === undefined) return null;

  // Marqué consommé avant toute autre écriture : un lien rejoué ne doit jamais
  // ouvrir une seconde session, même si la suite échoue.
  await ctx.db.update(magicLinks).set({ consumedAt: now }).where(eq(magicLinks.token, token));

  const existing = await ctx.db.select().from(users).where(eq(users.email, link.email)).limit(1);
  let userId = existing[0]?.id;
  if (userId === undefined) {
    const created = await ctx.db
      .insert(users)
      .values({ email: link.email, name: link.email.split("@")[0] ?? link.email })
      .returning({ id: users.id });
    userId = created[0]?.id;
    if (userId === undefined) throw new Error("création d'utilisateur sans identifiant retourné");
  }

  await ctx.db.update(invites).set({ usedAt: now }).where(eq(invites.email, link.email));

  const sessionToken = newToken();
  await ctx.db.insert(authSessions).values({
    token: sessionToken,
    userId,
    expiresAt: new Date(Date.now() + SESSION_TTL_MS),
  });
  return sessionToken;
}

export async function resolveUser(ctx: AppContext, token: string): Promise<AuthenticatedUser | null> {
  const rows = await ctx.db
    .select({ id: users.id, email: users.email, name: users.name })
    .from(authSessions)
    .innerJoin(users, eq(users.id, authSessions.userId))
    .where(and(eq(authSessions.token, token), gt(authSessions.expiresAt, new Date())))
    .limit(1);
  return rows[0] ?? null;
}

export function bearerFrom(request: FastifyRequest): string | null {
  const header = request.headers.authorization;
  if (header === undefined || !header.startsWith("Bearer ")) return null;
  const token = header.slice("Bearer ".length).trim();
  return token === "" ? null : token;
}

/** Hook `preHandler` à poser sur toute route qui touche des données de cours. */
export function requireAuth(ctx: AppContext) {
  return async (request: FastifyRequest, reply: FastifyReply): Promise<void> => {
    const token = bearerFrom(request);
    if (token === null) {
      await reply.code(401).send({ error: "authentification requise" });
      return;
    }
    const user = await resolveUser(ctx, token);
    if (user === null) {
      await reply.code(401).send({ error: "session expirée ou inconnue" });
      return;
    }
    request.user = user;
  };
}
