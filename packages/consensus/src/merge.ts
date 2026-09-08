import type { ClockModel } from "@amphi/shared";
import { toSessionMs } from "@amphi/shared";

/**
 * Fusion multi-flux — §5.2 et §5.3.
 *
 * ÉTAT : le chemin N = 1 est complet et c'est celui que M1 utilise. L'alignement
 * ROVER et le vote pondéré arrivent en M3 ; jusque-là, `merge` sur plusieurs flux
 * retient le meilleur et le signale dans `stats.mode`. Ce n'est pas une fusion
 * dégradée déguisée en fusion : l'appelant peut le voir, et le banc de test
 * l'assert explicitement.
 *
 * L'intérêt de poser l'API maintenant est que le worker appelle dès M1 la même
 * fonction qu'en M3 — le §5.2 exige que le pipeline soit identique à un seul
 * appareil, et c'est plus facile à tenir si on ne la contourne jamais.
 */

export interface Word {
  readonly text: string;
  readonly startMs: number;
  readonly endMs: number;
  readonly confidence: number;
}

export interface QualityScore {
  /** Rapport signal/bruit estimé côté client à partir du VAD. */
  readonly snr: number;
  readonly meanConfidence: number;
  readonly gapRatio: number;
}

export interface StreamTranscript {
  readonly participantId: string;
  readonly quality: QualityScore;
  /** Horloge du participant : les mots sont en temps LOCAL, convertis ici. */
  readonly clock: ClockModel;
  readonly words: readonly Word[];
}

export interface SegmentVariant {
  readonly text: string;
  readonly score: number;
  readonly participantIds: readonly string[];
}

export interface CanonicalSegmentDraft {
  readonly startMs: number;
  readonly endMs: number;
  readonly text: string;
  readonly consensusScore: number;
  readonly isDisputed: boolean;
  readonly variants: readonly SegmentVariant[];
}

export interface ConsensusStats {
  readonly mode: "single" | "best-stream" | "rover";
  readonly streamCount: number;
  readonly segmentCount: number;
  readonly disputedCount: number;
}

export interface CanonicalTranscript {
  readonly segments: readonly CanonicalSegmentDraft[];
  readonly stats: ConsensusStats;
}

export interface ConsensusOptions {
  /** En dessous, le segment est marqué disputé et affiché comme tel. */
  readonly disputeThreshold: number;
  /** Écart relatif minimal entre les deux meilleures variantes (M3). */
  readonly marginThreshold: number;
  /** Deux mots séparés de plus que ça ne peuvent pas s'aligner (M3). */
  readonly maxAlignmentDeltaMs: number;
  /** Silence à partir duquel on coupe un segment. */
  readonly segmentGapMs: number;
  /** Durée maximale d'un segment, pour garder des ancres cliquables utiles. */
  readonly maxSegmentMs: number;
  readonly lexicon?: readonly string[];
}

export const DEFAULT_OPTIONS: ConsensusOptions = {
  disputeThreshold: 0.6,
  marginThreshold: 0.15,
  maxAlignmentDeltaMs: 800,
  segmentGapMs: 700,
  maxSegmentMs: 15_000,
};

/** Poids d'un flux dans le vote : combine SNR, confiance moyenne et trous. */
export function streamWeight(quality: QualityScore): number {
  const snrScore = Math.min(1, Math.max(0, quality.snr / 30));
  return Math.max(
    0.01,
    0.5 * snrScore + 0.3 * quality.meanConfidence + 0.2 * (1 - quality.gapRatio),
  );
}

function toSessionTime(stream: StreamTranscript): Word[] {
  return stream.words
    .map((word) => ({
      text: word.text.trim(),
      startMs: toSessionMs(stream.clock, word.startMs),
      endMs: toSessionMs(stream.clock, word.endMs),
      confidence: word.confidence,
    }))
    .filter((word) => word.text !== "")
    .sort((a, b) => a.startMs - b.startMs);
}

/**
 * Regroupe les mots en segments aux silences. Les bornes servent d'ancres
 * cliquables dans les notes : trop longues, elles ne renvoient nulle part de
 * précis ; trop courtes, elles hachent la lecture.
 */
function groupIntoSegments(
  words: readonly Word[],
  options: ConsensusOptions,
): CanonicalSegmentDraft[] {
  const segments: CanonicalSegmentDraft[] = [];
  let current: Word[] = [];

  const flush = (): void => {
    if (current.length === 0) return;
    const first = current[0] as Word;
    const last = current[current.length - 1] as Word;
    const confidence = current.reduce((sum, w) => sum + w.confidence, 0) / current.length;
    segments.push({
      startMs: Math.round(first.startMs),
      endMs: Math.round(last.endMs),
      text: current.map((w) => w.text).join(" "),
      consensusScore: confidence,
      isDisputed: confidence < options.disputeThreshold,
      variants: [],
    });
    current = [];
  };

  for (const word of words) {
    const previous = current[current.length - 1];
    if (previous !== undefined) {
      const silence = word.startMs - previous.endMs;
      const spanTooLong = word.endMs - (current[0] as Word).startMs > options.maxSegmentMs;
      if (silence > options.segmentGapMs || spanTooLong) flush();
    }
    current.push(word);
  }
  flush();

  return segments;
}

export function merge(
  streams: readonly StreamTranscript[],
  overrides: Partial<ConsensusOptions> = {},
): CanonicalTranscript {
  const options: ConsensusOptions = { ...DEFAULT_OPTIONS, ...overrides };

  if (streams.length === 0) {
    return {
      segments: [],
      stats: { mode: "single", streamCount: 0, segmentCount: 0, disputedCount: 0 },
    };
  }

  // TODO(M3) : alignement progressif ROVER + vote pondéré sur le réseau de
  // confusion. En attendant, on retient le flux de plus fort poids — ce qui est
  // exactement ce que ferait ROVER avec un seul flux, et une borne basse
  // honnête avec plusieurs.
  const ranked = [...streams].sort((a, b) => streamWeight(b.quality) - streamWeight(a.quality));
  const best = ranked[0] as StreamTranscript;

  const segments = groupIntoSegments(toSessionTime(best), options);

  return {
    segments,
    stats: {
      mode: streams.length === 1 ? "single" : "best-stream",
      streamCount: streams.length,
      segmentCount: segments.length,
      disputedCount: segments.filter((s) => s.isDisputed).length,
    },
  };
}
