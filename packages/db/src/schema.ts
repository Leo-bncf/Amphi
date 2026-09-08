import {
  boolean,
  doublePrecision,
  index,
  integer,
  jsonb,
  pgEnum,
  pgTable,
  real,
  text,
  timestamp,
  unique,
  uuid,
  vector,
} from "drizzle-orm/pg-core";

/**
 * Schéma — §9 de l'ARCHITECTURE.
 *
 * Trois écarts par rapport au modèle du brief, tous justifiés là-bas :
 *   - `clockA` / `clockB` au lieu d'un simple `clock_offset_ms` : il faut la pente,
 *     pas seulement le décalage, sinon la dérive n'est pas corrigeable (ADR-03) ;
 *   - `envelope` sur les chunks : permet le ré-ancrage temporel même après
 *     suppression de l'audio, donc compatible avec le mode « transcription only » ;
 *   - `sessionEvents` et `costLedger`, absents du brief : le premier débloque le
 *     bouton « je n'ai pas compris » sans migration future, le second rend le
 *     budget pilotable autrement que sur la facture.
 */

export const sessionStatus = pgEnum("session_status", [
  "scheduled",
  "recording",
  "processing",
  "ready",
  "failed",
]);

export const costProfile = pgEnum("cost_profile", ["minimal", "suivi", "fusion", "max"]);
export const participantRole = pgEnum("participant_role", ["recorder", "viewer"]);
export const chunkStatus = pgEnum("chunk_status", [
  "pending",
  "uploaded",
  "transcribed",
  "failed",
  "deleted",
]);
export const consentMethod = pgEnum("consent_method", [
  "in_app_code",
  "declared_by_student",
  "written",
]);
export const sessionEventKind = pgEnum("session_event_kind", [
  "joined",
  "left",
  "not-understood",
  "marked-important",
  "gap-detected",
  "stream-desynced",
]);
export const costKind = pgEnum("cost_kind", [
  "asr",
  "llm-summary",
  "llm-document",
  "llm-flashcards",
  "embedding",
]);
export const attachmentType = pgEnum("attachment_type", ["slide", "photo", "diagram"]);

export const users = pgTable("users", {
  id: uuid("id").primaryKey().defaultRandom(),
  email: text("email").notNull().unique(),
  name: text("name").notNull(),
  school: text("school"),
  createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
});

/** Liste d'invités (ADR-10). Une adresse absente d'ici ne peut pas créer de compte. */
export const invites = pgTable("invites", {
  email: text("email").primaryKey(),
  invitedByUserId: uuid("invited_by_user_id").references(() => users.id, { onDelete: "set null" }),
  invitedAt: timestamp("invited_at", { withTimezone: true }).notNull().defaultNow(),
  usedAt: timestamp("used_at", { withTimezone: true }),
});

export const courses = pgTable(
  "courses",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    title: text("title").notNull(),
    code: text("code"),
    instructor: text("instructor"),
    icsUid: text("ics_uid"),
    lmsRef: text("lms_ref"),
    /** Vocabulaire injecté dans le prompt ASR : jargon, sigles, noms propres. */
    lexicon: jsonb("lexicon").$type<string[]>().notNull().default([]),
    costProfile: costProfile("cost_profile").notNull().default("fusion"),
    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => [index("courses_ics_uid_idx").on(t.icsUid)],
);

/**
 * Le consentement est porté par le COURS, pas par la séance : Leo a déjà l'accord
 * verbal des enseignants, on ne le redemande donc pas à chaque fois. Ce qui compte
 * juridiquement est la trace écrite, pas le canal (§10).
 */
export const consentRecords = pgTable("consent_records", {
  id: uuid("id").primaryKey().defaultRandom(),
  courseId: uuid("course_id")
    .notNull()
    .references(() => courses.id, { onDelete: "cascade" }),
  method: consentMethod("method").notNull(),
  grantedBy: text("granted_by").notNull(),
  grantedAt: timestamp("granted_at", { withTimezone: true }).notNull(),
  grantedToUserId: uuid("granted_to_user_id").references(() => users.id, { onDelete: "set null" }),
  /** Renseigner cette colonne déclenche la purge en cascade de la séance. */
  revokedAt: timestamp("revoked_at", { withTimezone: true }),
  note: text("note"),
});

export const sessions = pgTable(
  "sessions",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    courseId: uuid("course_id").references(() => courses.id, { onDelete: "set null" }),
    startedAt: timestamp("started_at", { withTimezone: true }).notNull().defaultNow(),
    endedAt: timestamp("ended_at", { withTimezone: true }),
    room: text("room"),
    status: sessionStatus("status").notNull().default("recording"),
    costProfile: costProfile("cost_profile").notNull().default("fusion"),
    /** Millièmes d'euro. Entier : jamais de flottant sur de la monnaie. */
    spentMilliCents: integer("spent_milli_cents").notNull().default(0),
    /** Code court affiché pour rejoindre la séance à proximité. */
    joinCode: text("join_code").notNull().unique(),
  },
  (t) => [index("sessions_course_started_idx").on(t.courseId, t.startedAt)],
);

export const participants = pgTable(
  "participants",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    sessionId: uuid("session_id")
      .notNull()
      .references(() => sessions.id, { onDelete: "cascade" }),
    userId: uuid("user_id")
      .notNull()
      .references(() => users.id, { onDelete: "cascade" }),
    role: participantRole("role").notNull().default("recorder"),
    deviceLabel: text("device_label").notNull(),
    /** t_session = clockA · t_local + clockB. La pente est indispensable (ADR-03). */
    clockA: doublePrecision("clock_a").notNull().default(1),
    clockB: doublePrecision("clock_b").notNull().default(0),
    audioQualityScore: real("audio_quality_score"),
    /** Faux pour un flux enregistré mais gardé dormant, transcriptible après coup. */
    isTranscribed: boolean("is_transcribed").notNull().default(false),
    joinedAt: timestamp("joined_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => [unique("participants_session_user_uq").on(t.sessionId, t.userId)],
);

export const audioChunks = pgTable(
  "audio_chunks",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    participantId: uuid("participant_id")
      .notNull()
      .references(() => participants.id, { onDelete: "cascade" }),
    seq: integer("seq").notNull(),
    startedAtSessionMs: integer("started_at_session_ms").notNull(),
    durationMs: integer("duration_ms").notNull(),
    storageKey: text("storage_key").notNull(),
    status: chunkStatus("status").notNull().default("pending"),
    /** Vrai si le compteur d'échantillons a sauté : chunk exclu du consensus. */
    hasGap: boolean("has_gap").notNull().default(false),
    /**
     * Enveloppe d'énergie 50 Hz quantifiée uint8, ~180 ko/h. Sert au ré-ancrage
     * temporel et survit à la suppression de l'audio — c'est ce qui rend le mode
     * « transcription only » compatible avec la fusion.
     */
    envelope: text("envelope"),
    deleteAfter: timestamp("delete_after", { withTimezone: true }),
    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => [
    // Clé d'idempotence de la file offline : un chunk rejoué ne crée pas de doublon.
    unique("audio_chunks_participant_seq_uq").on(t.participantId, t.seq),
    index("audio_chunks_status_idx").on(t.status),
    index("audio_chunks_delete_after_idx").on(t.deleteAfter),
  ],
);

export const transcriptSegments = pgTable(
  "transcript_segments",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    sessionId: uuid("session_id")
      .notNull()
      .references(() => sessions.id, { onDelete: "cascade" }),
    participantId: uuid("participant_id")
      .notNull()
      .references(() => participants.id, { onDelete: "cascade" }),
    /** Déjà converti en temps de session via le modèle d'horloge du participant. */
    startMs: integer("start_ms").notNull(),
    endMs: integer("end_ms").notNull(),
    text: text("text").notNull(),
    confidence: real("confidence").notNull(),
    lang: text("lang"),
    /** Horodatages au mot — indispensables à l'alignement ROVER. */
    tokens: jsonb("tokens")
      .$type<{ text: string; startMs: number; endMs: number; confidence: number }[]>()
      .notNull(),
    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => [index("transcript_segments_session_start_idx").on(t.sessionId, t.startMs)],
);

export const canonicalSegments = pgTable(
  "canonical_segments",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    sessionId: uuid("session_id")
      .notNull()
      .references(() => sessions.id, { onDelete: "cascade" }),
    startMs: integer("start_ms").notNull(),
    endMs: integer("end_ms").notNull(),
    text: text("text").notNull(),
    consensusScore: real("consensus_score").notNull(),
    isDisputed: boolean("is_disputed").notNull().default(false),
    /** Variantes concurrentes avec leur provenance, pour l'arbitrage humain. */
    variants: jsonb("variants")
      .$type<{ text: string; score: number; participantIds: string[] }[]>()
      .notNull()
      .default([]),
    /** Un arbitrage humain devient la vérité et prime sur le vote. */
    resolvedByUserId: uuid("resolved_by_user_id").references(() => users.id, { onDelete: "set null" }),
    resolvedAt: timestamp("resolved_at", { withTimezone: true }),
  },
  (t) => [
    index("canonical_segments_session_start_idx").on(t.sessionId, t.startMs),
    index("canonical_segments_disputed_idx").on(t.sessionId, t.isDisputed),
  ],
);

export const noteDocs = pgTable("note_docs", {
  id: uuid("id").primaryKey().defaultRandom(),
  sessionId: uuid("session_id")
    .notNull()
    .unique()
    .references(() => sessions.id, { onDelete: "cascade" }),
  /** État Yjs sérialisé. Le CRDT est la source de vérité de l'édition. */
  ydoc: text("ydoc"),
  updatedAt: timestamp("updated_at", { withTimezone: true }).notNull().defaultNow(),
});

export const noteSnapshots = pgTable(
  "note_snapshots",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    noteDocId: uuid("note_doc_id")
      .notNull()
      .references(() => noteDocs.id, { onDelete: "cascade" }),
    ydoc: text("ydoc").notNull(),
    label: text("label"),
    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => [index("note_snapshots_doc_created_idx").on(t.noteDocId, t.createdAt)],
);

export const attachments = pgTable(
  "attachments",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    sessionId: uuid("session_id")
      .notNull()
      .references(() => sessions.id, { onDelete: "cascade" }),
    type: attachmentType("type").notNull(),
    storageKey: text("storage_key").notNull(),
    extractedText: text("extracted_text"),
    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => [index("attachments_session_idx").on(t.sessionId)],
);

/** Recherche sémantique globale (§3.8). bge-m3 sort en 1024 dimensions. */
export const embeddings = pgTable(
  "embeddings",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    sourceType: text("source_type").notNull(),
    sourceId: uuid("source_id").notNull(),
    chunkText: text("chunk_text").notNull(),
    vector: vector("vector", { dimensions: 1024 }).notNull(),
  },
  (t) => [
    index("embeddings_source_idx").on(t.sourceType, t.sourceId),
    index("embeddings_vector_idx").using("hnsw", t.vector.op("vector_cosine_ops")),
  ],
);

/**
 * Événement daté générique. Existe dès M1 pour ne pas avoir à migrer quand
 * arrivera le bouton « je n'ai pas compris » (§9 du brief).
 */
export const sessionEvents = pgTable(
  "session_events",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    sessionId: uuid("session_id")
      .notNull()
      .references(() => sessions.id, { onDelete: "cascade" }),
    userId: uuid("user_id").references(() => users.id, { onDelete: "set null" }),
    kind: sessionEventKind("kind").notNull(),
    atSessionMs: integer("at_session_ms").notNull(),
    payload: jsonb("payload").$type<Record<string, unknown>>().notNull().default({}),
    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => [index("session_events_session_at_idx").on(t.sessionId, t.atSessionMs, t.kind)],
);

/** Sans ce registre, le budget se découvrirait sur la facture (§11). */
export const costLedger = pgTable(
  "cost_ledger",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    sessionId: uuid("session_id").references(() => sessions.id, { onDelete: "set null" }),
    kind: costKind("kind").notNull(),
    provider: text("provider").notNull(),
    model: text("model").notNull(),
    /** Secondes d'audio pour l'ASR, tokens pour le LLM. */
    units: doublePrecision("units").notNull(),
    costMilliCents: integer("cost_milli_cents").notNull(),
    at: timestamp("at", { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => [index("cost_ledger_at_idx").on(t.at), index("cost_ledger_session_idx").on(t.sessionId)],
);

/** Droit à l'effacement (§10) — traçable et vérifiable, pas un simple DELETE. */
export const deletionRequests = pgTable("deletion_requests", {
  id: uuid("id").primaryKey().defaultRandom(),
  userId: uuid("user_id")
    .notNull()
    .references(() => users.id, { onDelete: "cascade" }),
  scope: text("scope").notNull(),
  requestedAt: timestamp("requested_at", { withTimezone: true }).notNull().defaultNow(),
  completedAt: timestamp("completed_at", { withTimezone: true }),
  report: jsonb("report").$type<Record<string, number>>(),
});

/** Sessions d'authentification par lien magique (ADR-10). */
export const magicLinks = pgTable(
  "magic_links",
  {
    token: text("token").primaryKey(),
    email: text("email").notNull(),
    expiresAt: timestamp("expires_at", { withTimezone: true }).notNull(),
    consumedAt: timestamp("consumed_at", { withTimezone: true }),
  },
  (t) => [index("magic_links_email_idx").on(t.email)],
);

export const authSessions = pgTable(
  "auth_sessions",
  {
    token: text("token").primaryKey(),
    userId: uuid("user_id")
      .notNull()
      .references(() => users.id, { onDelete: "cascade" }),
    expiresAt: timestamp("expires_at", { withTimezone: true }).notNull(),
    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => [index("auth_sessions_user_idx").on(t.userId)],
);

export const schema = {
  users,
  invites,
  courses,
  consentRecords,
  sessions,
  participants,
  audioChunks,
  transcriptSegments,
  canonicalSegments,
  noteDocs,
  noteSnapshots,
  attachments,
  embeddings,
  sessionEvents,
  costLedger,
  deletionRequests,
  magicLinks,
  authSessions,
};
