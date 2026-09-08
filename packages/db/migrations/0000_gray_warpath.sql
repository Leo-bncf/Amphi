CREATE TYPE "public"."attachment_type" AS ENUM('slide', 'photo', 'diagram');--> statement-breakpoint
CREATE TYPE "public"."chunk_status" AS ENUM('pending', 'uploaded', 'transcribed', 'failed', 'deleted');--> statement-breakpoint
CREATE TYPE "public"."consent_method" AS ENUM('in_app_code', 'declared_by_student', 'written');--> statement-breakpoint
CREATE TYPE "public"."cost_kind" AS ENUM('asr', 'llm-summary', 'llm-document', 'llm-flashcards', 'embedding');--> statement-breakpoint
CREATE TYPE "public"."cost_profile" AS ENUM('minimal', 'suivi', 'fusion', 'max');--> statement-breakpoint
CREATE TYPE "public"."participant_role" AS ENUM('recorder', 'viewer');--> statement-breakpoint
CREATE TYPE "public"."session_event_kind" AS ENUM('joined', 'left', 'not-understood', 'marked-important', 'gap-detected', 'stream-desynced');--> statement-breakpoint
CREATE TYPE "public"."session_status" AS ENUM('scheduled', 'recording', 'processing', 'ready', 'failed');--> statement-breakpoint
CREATE TABLE "attachments" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"session_id" uuid NOT NULL,
	"type" "attachment_type" NOT NULL,
	"storage_key" text NOT NULL,
	"extracted_text" text,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "audio_chunks" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"participant_id" uuid NOT NULL,
	"seq" integer NOT NULL,
	"started_at_session_ms" integer NOT NULL,
	"duration_ms" integer NOT NULL,
	"storage_key" text NOT NULL,
	"status" "chunk_status" DEFAULT 'pending' NOT NULL,
	"has_gap" boolean DEFAULT false NOT NULL,
	"envelope" text,
	"delete_after" timestamp with time zone,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "audio_chunks_participant_seq_uq" UNIQUE("participant_id","seq")
);
--> statement-breakpoint
CREATE TABLE "auth_sessions" (
	"token" text PRIMARY KEY NOT NULL,
	"user_id" uuid NOT NULL,
	"expires_at" timestamp with time zone NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "canonical_segments" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"session_id" uuid NOT NULL,
	"start_ms" integer NOT NULL,
	"end_ms" integer NOT NULL,
	"text" text NOT NULL,
	"consensus_score" real NOT NULL,
	"is_disputed" boolean DEFAULT false NOT NULL,
	"variants" jsonb DEFAULT '[]'::jsonb NOT NULL,
	"resolved_by_user_id" uuid,
	"resolved_at" timestamp with time zone
);
--> statement-breakpoint
CREATE TABLE "consent_records" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"course_id" uuid NOT NULL,
	"method" "consent_method" NOT NULL,
	"granted_by" text NOT NULL,
	"granted_at" timestamp with time zone NOT NULL,
	"granted_to_user_id" uuid,
	"revoked_at" timestamp with time zone,
	"note" text
);
--> statement-breakpoint
CREATE TABLE "cost_ledger" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"session_id" uuid,
	"kind" "cost_kind" NOT NULL,
	"provider" text NOT NULL,
	"model" text NOT NULL,
	"units" double precision NOT NULL,
	"cost_milli_cents" integer NOT NULL,
	"at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "courses" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"title" text NOT NULL,
	"code" text,
	"instructor" text,
	"ics_uid" text,
	"lms_ref" text,
	"lexicon" jsonb DEFAULT '[]'::jsonb NOT NULL,
	"cost_profile" "cost_profile" DEFAULT 'fusion' NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "deletion_requests" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"user_id" uuid NOT NULL,
	"scope" text NOT NULL,
	"requested_at" timestamp with time zone DEFAULT now() NOT NULL,
	"completed_at" timestamp with time zone,
	"report" jsonb
);
--> statement-breakpoint
CREATE TABLE "embeddings" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"source_type" text NOT NULL,
	"source_id" uuid NOT NULL,
	"chunk_text" text NOT NULL,
	"vector" vector(1024) NOT NULL
);
--> statement-breakpoint
CREATE TABLE "invites" (
	"email" text PRIMARY KEY NOT NULL,
	"invited_by_user_id" uuid,
	"invited_at" timestamp with time zone DEFAULT now() NOT NULL,
	"used_at" timestamp with time zone
);
--> statement-breakpoint
CREATE TABLE "magic_links" (
	"token" text PRIMARY KEY NOT NULL,
	"email" text NOT NULL,
	"expires_at" timestamp with time zone NOT NULL,
	"consumed_at" timestamp with time zone
);
--> statement-breakpoint
CREATE TABLE "note_docs" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"session_id" uuid NOT NULL,
	"ydoc" text,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "note_docs_session_id_unique" UNIQUE("session_id")
);
--> statement-breakpoint
CREATE TABLE "note_snapshots" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"note_doc_id" uuid NOT NULL,
	"ydoc" text NOT NULL,
	"label" text,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "participants" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"session_id" uuid NOT NULL,
	"user_id" uuid NOT NULL,
	"role" "participant_role" DEFAULT 'recorder' NOT NULL,
	"device_label" text NOT NULL,
	"clock_a" double precision DEFAULT 1 NOT NULL,
	"clock_b" double precision DEFAULT 0 NOT NULL,
	"audio_quality_score" real,
	"is_transcribed" boolean DEFAULT false NOT NULL,
	"joined_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "participants_session_user_uq" UNIQUE("session_id","user_id")
);
--> statement-breakpoint
CREATE TABLE "session_events" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"session_id" uuid NOT NULL,
	"user_id" uuid,
	"kind" "session_event_kind" NOT NULL,
	"at_session_ms" integer NOT NULL,
	"payload" jsonb DEFAULT '{}'::jsonb NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "sessions" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"course_id" uuid,
	"started_at" timestamp with time zone DEFAULT now() NOT NULL,
	"ended_at" timestamp with time zone,
	"room" text,
	"status" "session_status" DEFAULT 'recording' NOT NULL,
	"cost_profile" "cost_profile" DEFAULT 'fusion' NOT NULL,
	"spent_milli_cents" integer DEFAULT 0 NOT NULL,
	"join_code" text NOT NULL,
	CONSTRAINT "sessions_join_code_unique" UNIQUE("join_code")
);
--> statement-breakpoint
CREATE TABLE "transcript_segments" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"session_id" uuid NOT NULL,
	"participant_id" uuid NOT NULL,
	"start_ms" integer NOT NULL,
	"end_ms" integer NOT NULL,
	"text" text NOT NULL,
	"confidence" real NOT NULL,
	"lang" text,
	"tokens" jsonb NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "users" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"email" text NOT NULL,
	"name" text NOT NULL,
	"school" text,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "users_email_unique" UNIQUE("email")
);
--> statement-breakpoint
ALTER TABLE "attachments" ADD CONSTRAINT "attachments_session_id_sessions_id_fk" FOREIGN KEY ("session_id") REFERENCES "public"."sessions"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "audio_chunks" ADD CONSTRAINT "audio_chunks_participant_id_participants_id_fk" FOREIGN KEY ("participant_id") REFERENCES "public"."participants"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "auth_sessions" ADD CONSTRAINT "auth_sessions_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."users"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "canonical_segments" ADD CONSTRAINT "canonical_segments_session_id_sessions_id_fk" FOREIGN KEY ("session_id") REFERENCES "public"."sessions"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "canonical_segments" ADD CONSTRAINT "canonical_segments_resolved_by_user_id_users_id_fk" FOREIGN KEY ("resolved_by_user_id") REFERENCES "public"."users"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "consent_records" ADD CONSTRAINT "consent_records_course_id_courses_id_fk" FOREIGN KEY ("course_id") REFERENCES "public"."courses"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "consent_records" ADD CONSTRAINT "consent_records_granted_to_user_id_users_id_fk" FOREIGN KEY ("granted_to_user_id") REFERENCES "public"."users"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "cost_ledger" ADD CONSTRAINT "cost_ledger_session_id_sessions_id_fk" FOREIGN KEY ("session_id") REFERENCES "public"."sessions"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "deletion_requests" ADD CONSTRAINT "deletion_requests_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."users"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "invites" ADD CONSTRAINT "invites_invited_by_user_id_users_id_fk" FOREIGN KEY ("invited_by_user_id") REFERENCES "public"."users"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "note_docs" ADD CONSTRAINT "note_docs_session_id_sessions_id_fk" FOREIGN KEY ("session_id") REFERENCES "public"."sessions"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "note_snapshots" ADD CONSTRAINT "note_snapshots_note_doc_id_note_docs_id_fk" FOREIGN KEY ("note_doc_id") REFERENCES "public"."note_docs"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "participants" ADD CONSTRAINT "participants_session_id_sessions_id_fk" FOREIGN KEY ("session_id") REFERENCES "public"."sessions"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "participants" ADD CONSTRAINT "participants_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."users"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "session_events" ADD CONSTRAINT "session_events_session_id_sessions_id_fk" FOREIGN KEY ("session_id") REFERENCES "public"."sessions"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "session_events" ADD CONSTRAINT "session_events_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."users"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "sessions" ADD CONSTRAINT "sessions_course_id_courses_id_fk" FOREIGN KEY ("course_id") REFERENCES "public"."courses"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "transcript_segments" ADD CONSTRAINT "transcript_segments_session_id_sessions_id_fk" FOREIGN KEY ("session_id") REFERENCES "public"."sessions"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "transcript_segments" ADD CONSTRAINT "transcript_segments_participant_id_participants_id_fk" FOREIGN KEY ("participant_id") REFERENCES "public"."participants"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
CREATE INDEX "attachments_session_idx" ON "attachments" USING btree ("session_id");--> statement-breakpoint
CREATE INDEX "audio_chunks_status_idx" ON "audio_chunks" USING btree ("status");--> statement-breakpoint
CREATE INDEX "audio_chunks_delete_after_idx" ON "audio_chunks" USING btree ("delete_after");--> statement-breakpoint
CREATE INDEX "auth_sessions_user_idx" ON "auth_sessions" USING btree ("user_id");--> statement-breakpoint
CREATE INDEX "canonical_segments_session_start_idx" ON "canonical_segments" USING btree ("session_id","start_ms");--> statement-breakpoint
CREATE INDEX "canonical_segments_disputed_idx" ON "canonical_segments" USING btree ("session_id","is_disputed");--> statement-breakpoint
CREATE INDEX "cost_ledger_at_idx" ON "cost_ledger" USING btree ("at");--> statement-breakpoint
CREATE INDEX "cost_ledger_session_idx" ON "cost_ledger" USING btree ("session_id");--> statement-breakpoint
CREATE INDEX "courses_ics_uid_idx" ON "courses" USING btree ("ics_uid");--> statement-breakpoint
CREATE INDEX "embeddings_source_idx" ON "embeddings" USING btree ("source_type","source_id");--> statement-breakpoint
CREATE INDEX "embeddings_vector_idx" ON "embeddings" USING hnsw ("vector" vector_cosine_ops);--> statement-breakpoint
CREATE INDEX "magic_links_email_idx" ON "magic_links" USING btree ("email");--> statement-breakpoint
CREATE INDEX "note_snapshots_doc_created_idx" ON "note_snapshots" USING btree ("note_doc_id","created_at");--> statement-breakpoint
CREATE INDEX "session_events_session_at_idx" ON "session_events" USING btree ("session_id","at_session_ms","kind");--> statement-breakpoint
CREATE INDEX "sessions_course_started_idx" ON "sessions" USING btree ("course_id","started_at");--> statement-breakpoint
CREATE INDEX "transcript_segments_session_start_idx" ON "transcript_segments" USING btree ("session_id","start_ms");