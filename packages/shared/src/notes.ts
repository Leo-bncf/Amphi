import { z } from "zod";

/**
 * Document de notes structuré — §6.2 et §6.3.
 *
 * La règle non négociable est l'ancrage : chaque bloc porte les identifiants des
 * segments de transcription dont il est tiré, et un bloc dont l'ancre ne survit
 * pas à la vérification est ÉCARTÉ, pas affiché. C'est ce qui distingue des notes
 * qu'on peut réviser d'un résumé qu'il faut croire sur parole.
 */

export const NoteCalloutKind = z.enum(["a-retenir", "exemple", "attention", "question-ouverte"]);
export type NoteCalloutKind = z.infer<typeof NoteCalloutKind>;

/** Référence vers la transcription. Non vide, sinon le bloc est rejeté. */
export const SourceAnchor = z.object({
  segmentIds: z.array(z.string()).min(1),
  startMs: z.number().nonnegative(),
  endMs: z.number().nonnegative(),
});
export type SourceAnchor = z.infer<typeof SourceAnchor>;

const withAnchor = { anchor: SourceAnchor };

export const NoteBlock = z.discriminatedUnion("type", [
  z.object({ type: z.literal("heading"), level: z.union([z.literal(1), z.literal(2), z.literal(3)]), text: z.string(), ...withAnchor }),
  z.object({ type: z.literal("paragraph"), text: z.string(), ...withAnchor }),
  z.object({ type: z.literal("bullets"), items: z.array(z.string()).min(1), ...withAnchor }),
  z.object({ type: z.literal("definition"), term: z.string(), definition: z.string(), ...withAnchor }),
  /** LaTeX brut, rendu par KaTeX. `display` = formule centrée sur sa propre ligne. */
  z.object({ type: z.literal("formula"), latex: z.string(), display: z.boolean(), caption: z.string().nullable(), ...withAnchor }),
  z.object({ type: z.literal("callout"), kind: NoteCalloutKind, text: z.string(), ...withAnchor }),
]);
export type NoteBlock = z.infer<typeof NoteBlock>;

export const GlossaryEntry = z.object({
  term: z.string(),
  definition: z.string(),
  anchor: SourceAnchor,
});
export type GlossaryEntry = z.infer<typeof GlossaryEntry>;

/** Ce que le LLM doit produire. Contraint par structured output, pas par un prompt poli. */
export const GeneratedNoteDocument = z.object({
  title: z.string(),
  summary: z.string(),
  blocks: z.array(NoteBlock),
  glossary: z.array(GlossaryEntry),
  openQuestions: z.array(z.object({ question: z.string(), anchor: SourceAnchor })),
});
export type GeneratedNoteDocument = z.infer<typeof GeneratedNoteDocument>;

/** Résumé incrémental affiché pendant le cours (§6.1). Non injecté dans le document. */
export const IncrementalSummary = z.object({
  runningSummary: z.string(),
  newPoints: z.array(z.string()),
  newTerms: z.array(z.string()),
  coveredUntilMs: z.number().nonnegative(),
});
export type IncrementalSummary = z.infer<typeof IncrementalSummary>;

export const Flashcard = z.object({
  front: z.string(),
  back: z.string(),
  anchor: SourceAnchor,
});
export type Flashcard = z.infer<typeof Flashcard>;
