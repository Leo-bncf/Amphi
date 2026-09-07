import { z } from "zod";

/**
 * Interface de transcription — ADR-01.
 *
 * Trois implémentations prévues, interchangeables par configuration :
 *   1. `mlx-whisper` local sur le Mac (gratuit, défaut quand le worker est réveillé)
 *   2. Groq `whisper-large-v3-turbo` (repli payant, 0,037 €/h)
 *   3. OVHcloud AI Endpoints (repli 100 % UE, 0,046 €/h)
 *
 * Le contrat impose des horodatages au mot : le module de consensus en a besoin
 * pour aligner les flux, et les notes générées en ont besoin pour être traçables.
 */

export const AsrWord = z.object({
  text: z.string(),
  startMs: z.number().nonnegative(),
  endMs: z.number().nonnegative(),
  /** 0 à 1. Les moteurs qui n'en fournissent pas doivent renvoyer une estimation, jamais 1 par défaut. */
  confidence: z.number().min(0).max(1),
});
export type AsrWord = z.infer<typeof AsrWord>;

export const AsrSegment = z.object({
  startMs: z.number().nonnegative(),
  endMs: z.number().nonnegative(),
  text: z.string(),
  words: z.array(AsrWord),
  /** Code BCP-47 détecté. Jamais imposé : les cours mélangent français et anglais. */
  lang: z.string().nullable(),
  avgConfidence: z.number().min(0).max(1),
});
export type AsrSegment = z.infer<typeof AsrSegment>;

export const AsrResult = z.object({
  segments: z.array(AsrSegment),
  audioDurationMs: z.number().nonnegative(),
  provider: z.string(),
  model: z.string(),
  /** Coût réel de l'appel, en millièmes d'euro. 0 pour un moteur local. */
  costMilliCents: z.number().nonnegative(),
  processingMs: z.number().nonnegative(),
});
export type AsrResult = z.infer<typeof AsrResult>;

export interface AsrRequest {
  readonly audio: Uint8Array;
  /** `audio/ogg; codecs=opus` en nominal, `audio/wav` pour les bancs de test. */
  readonly mimeType: string;
  /**
   * Vocabulaire du cours injecté dans le prompt du modèle : titre, nom de
   * l'intervenant, termes du syllabus, notions des séances précédentes.
   * C'est ce qui fait la différence sur « gradient boosting » ou « hétéroscédasticité ».
   */
  readonly lexicon?: readonly string[];
  /** Fin de la transcription précédente, pour la continuité entre chunks. */
  readonly previousText?: string;
}

export interface AsrProvider {
  readonly name: string;
  readonly isLocal: boolean;
  transcribe(request: AsrRequest): Promise<AsrResult>;
  /** Le worker local répond false quand la machine dort ou est sur batterie faible. */
  isAvailable(): Promise<boolean>;
}
