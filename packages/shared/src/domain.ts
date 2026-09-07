import { z } from "zod";
import {
  AudioChunkId,
  CanonicalSegmentId,
  CourseId,
  ParticipantId,
  SessionId,
  TranscriptSegmentId,
  UserId,
} from "./ids.js";

/** Entités du §9 de l'architecture. Ces schémas sont la frontière : rien n'entre sans validation. */

export const SessionStatus = z.enum(["scheduled", "recording", "processing", "ready", "failed"]);
export type SessionStatus = z.infer<typeof SessionStatus>;

/** Profil de coût, choisi PAR COURS et modifiable rétroactivement (§11.4). */
export const CostProfile = z.enum(["minimal", "suivi", "fusion", "max"]);
export type CostProfile = z.infer<typeof CostProfile>;

export const ParticipantRole = z.enum(["recorder", "viewer"]);
export type ParticipantRole = z.infer<typeof ParticipantRole>;

export const ChunkStatus = z.enum(["pending", "uploaded", "transcribed", "failed", "deleted"]);
export type ChunkStatus = z.infer<typeof ChunkStatus>;

export const Course = z.object({
  id: CourseId,
  title: z.string().min(1),
  code: z.string().nullable(),
  instructor: z.string().nullable(),
  icsUid: z.string().nullable(),
  lmsRef: z.string().nullable(),
  /** Vocabulaire injecté dans le prompt ASR : jargon, noms propres, sigles. */
  lexicon: z.array(z.string()),
  costProfile: CostProfile,
});
export type Course = z.infer<typeof Course>;

export const Session = z.object({
  id: SessionId,
  courseId: CourseId.nullable(),
  startedAt: z.date(),
  endedAt: z.date().nullable(),
  room: z.string().nullable(),
  status: SessionStatus,
  costProfile: CostProfile,
  /** Alimenté par le CostLedger. En millièmes d'euro. */
  spentMilliCents: z.number().nonnegative(),
});
export type Session = z.infer<typeof Session>;

export const Participant = z.object({
  sessionId: SessionId,
  id: ParticipantId,
  userId: UserId,
  role: ParticipantRole,
  deviceLabel: z.string(),
  /** Modèle affine d'horloge — voir clock.ts. Pente ET décalage, pas seulement le décalage. */
  clockA: z.number(),
  clockB: z.number(),
  /** Score composite SNR / énergie vocale / taux de trous. Décide quels flux sont transcrits. */
  audioQualityScore: z.number().min(0).max(1).nullable(),
  /** Faux pour un flux enregistré mais gardé dormant : il pourra être transcrit après coup. */
  isTranscribed: z.boolean(),
});
export type Participant = z.infer<typeof Participant>;

export const AudioChunk = z.object({
  id: AudioChunkId,
  participantId: ParticipantId,
  /** Unique avec participantId : c'est la clé d'idempotence de la file offline. */
  seq: z.number().int().nonnegative(),
  startedAtSessionMs: z.number().nonnegative(),
  durationMs: z.number().positive(),
  storageKey: z.string(),
  status: ChunkStatus,
  /** Vrai si le compteur d'échantillons a sauté : chunk exclu du consensus. */
  hasGap: z.boolean(),
  deleteAfter: z.date().nullable(),
});
export type AudioChunk = z.infer<typeof AudioChunk>;

export const TranscriptSegment = z.object({
  id: TranscriptSegmentId,
  sessionId: SessionId,
  participantId: ParticipantId,
  /** Déjà converti en temps de session via le modèle d'horloge du participant. */
  startMs: z.number().nonnegative(),
  endMs: z.number().nonnegative(),
  text: z.string(),
  confidence: z.number().min(0).max(1),
  lang: z.string().nullable(),
});
export type TranscriptSegment = z.infer<typeof TranscriptSegment>;

/** Une variante concurrente sur un segment disputé, avec sa provenance. */
export const SegmentVariant = z.object({
  text: z.string(),
  score: z.number(),
  participantIds: z.array(ParticipantId),
});
export type SegmentVariant = z.infer<typeof SegmentVariant>;

export const CanonicalSegment = z.object({
  id: CanonicalSegmentId,
  sessionId: SessionId,
  startMs: z.number().nonnegative(),
  endMs: z.number().nonnegative(),
  text: z.string(),
  consensusScore: z.number().min(0).max(1),
  isDisputed: z.boolean(),
  variants: z.array(SegmentVariant),
  /** Un arbitrage humain devient la vérité et prime sur le vote. */
  resolvedByUserId: UserId.nullable(),
  resolvedAt: z.date().nullable(),
});
export type CanonicalSegment = z.infer<typeof CanonicalSegment>;

/** Trace du consentement (§10). Sans elle, aucun AudioChunk n'est créé. */
export const ConsentMethod = z.enum(["in_app_code", "declared_by_student", "written"]);
export type ConsentMethod = z.infer<typeof ConsentMethod>;

export const ConsentRecord = z.object({
  courseId: CourseId,
  method: ConsentMethod,
  grantedBy: z.string(),
  grantedAt: z.date(),
  grantedToUserId: UserId,
  revokedAt: z.date().nullable(),
  note: z.string().nullable(),
});
export type ConsentRecord = z.infer<typeof ConsentRecord>;

/**
 * Événement générique daté sur la timeline de session. Existe dès M1 pour ne pas
 * avoir à migrer quand arrivera le bouton « je n'ai pas compris » (§9 du brief).
 */
export const SessionEventKind = z.enum([
  "joined",
  "left",
  "not-understood",
  "marked-important",
  "gap-detected",
  "stream-desynced",
]);
export type SessionEventKind = z.infer<typeof SessionEventKind>;

export const SessionEvent = z.object({
  sessionId: SessionId,
  userId: UserId.nullable(),
  kind: SessionEventKind,
  atSessionMs: z.number().nonnegative(),
  payload: z.record(z.string(), z.unknown()),
  createdAt: z.date(),
});
export type SessionEvent = z.infer<typeof SessionEvent>;

/** Sans ce registre, le budget se découvre sur la facture (§11). */
export const CostKind = z.enum(["asr", "llm-summary", "llm-document", "llm-flashcards", "embedding"]);
export type CostKind = z.infer<typeof CostKind>;

export const CostLedgerEntry = z.object({
  sessionId: SessionId,
  kind: CostKind,
  provider: z.string(),
  model: z.string(),
  units: z.number().nonnegative(),
  costMilliCents: z.number().nonnegative(),
  at: z.date(),
});
export type CostLedgerEntry = z.infer<typeof CostLedgerEntry>;
