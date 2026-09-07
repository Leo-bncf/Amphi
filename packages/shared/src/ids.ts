import { z } from "zod";

/**
 * Identifiants typés (branded). Empêche de passer un SessionId là où un
 * ParticipantId est attendu — les deux sont des UUID, le compilateur est
 * la seule chose qui puisse les distinguer.
 */

export const UserId = z.uuid().brand<"UserId">();
export const CourseId = z.uuid().brand<"CourseId">();
export const SessionId = z.uuid().brand<"SessionId">();
export const ParticipantId = z.uuid().brand<"ParticipantId">();
export const AudioChunkId = z.uuid().brand<"AudioChunkId">();
export const TranscriptSegmentId = z.uuid().brand<"TranscriptSegmentId">();
export const CanonicalSegmentId = z.uuid().brand<"CanonicalSegmentId">();
export const NoteDocId = z.uuid().brand<"NoteDocId">();
export const AttachmentId = z.uuid().brand<"AttachmentId">();

export type UserId = z.infer<typeof UserId>;
export type CourseId = z.infer<typeof CourseId>;
export type SessionId = z.infer<typeof SessionId>;
export type ParticipantId = z.infer<typeof ParticipantId>;
export type AudioChunkId = z.infer<typeof AudioChunkId>;
export type TranscriptSegmentId = z.infer<typeof TranscriptSegmentId>;
export type CanonicalSegmentId = z.infer<typeof CanonicalSegmentId>;
export type NoteDocId = z.infer<typeof NoteDocId>;
export type AttachmentId = z.infer<typeof AttachmentId>;
