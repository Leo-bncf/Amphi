import type { ClockModel } from "@amphi/shared";
import { toSessionMs } from "@amphi/shared";

/** Fusion déterministe de transcriptions par réseau de confusion (ROVER). */

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
  /** Horloge officielle : t_session = a * t_local + b. */
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
  readonly avgConfidence?: number;
  readonly consensusScore: number;
  readonly isDisputed: boolean;
  readonly variants: readonly SegmentVariant[];
  /** Provenance: unique (contributor, recording) pairs that contributed to this segment. */
  readonly sources: readonly { readonly by: string; readonly rec: string }[];
  /** Detailed word-level provenance: original segment/word/time positions. */
  readonly support: readonly {
    readonly by: string;
    readonly rec: string;
    readonly segment: number;
    readonly word: number;
    readonly startMs: number;
    readonly endMs: number;
  }[];
  /** Unique contributors for this segment. */
  readonly by: readonly string[];
  /** Unique recording IDs for this segment. */
  readonly rec: readonly string[];
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
  /** Écart relatif minimal entre les deux meilleures variantes. */
  readonly marginThreshold: number;
  /** Deux mots séparés de plus que ça ne peuvent pas s'aligner. */
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

function finite(value: number, fallback: number): number {
  return Number.isFinite(value) ? value : fallback;
}

function bounded(value: number, fallback = 0.5): number {
  return Math.max(0, Math.min(0.99, finite(value, fallback)));
}

function normalize(text: string): string {
  return text
    .normalize("NFKD")
    .toLocaleLowerCase("und")
    .replace(/[̀-ͯ]/gu, "")
    .replace(/[^\p{L}\p{N}_]+/gu, "");
}

interface TimedWord extends Word {
  readonly norm: string;
}

interface RoverStream {
  readonly participantId: string;
  readonly quality: number;
  readonly signature: string;
  words: TimedWord[];
  startMs: number;
  endMs: number;
  anchors: number;
}

interface Observation extends TimedWord {
  readonly participantId: string;
  readonly streamKey: string;
  readonly weight: number;
}

interface Slot {
  readonly observations: Observation[];
}

interface Alternative {
  readonly norm: string;
  readonly text: string;
  readonly total: number;
  readonly observations: readonly Observation[];
}

interface VotedWord {
  readonly deleted: boolean;
  readonly text: string;
  readonly startMs: number;
  readonly endMs: number;
  readonly confidence: number;
  readonly score: number;
  readonly disputed: boolean;
  readonly alternatives: readonly Alternative[];
  /** Provenance: unique (contributor, recording) pairs that contributed to this word. */
  readonly sources: readonly { readonly by: string; readonly rec: string }[];
  /** Detailed word-level provenance: original segment/word/time positions. */
  readonly support: readonly {
    readonly by: string;
    readonly rec: string;
    readonly segment: number;
    readonly word: number;
    readonly startMs: number;
    readonly endMs: number;
  }[];
  readonly by: readonly string[];
  readonly rec: readonly string[];
}

function toSessionTime(stream: StreamTranscript): TimedWord[] {
  return stream.words
    .map((word) => {
      const text = word.text.trim();
      const startMs = toSessionMs(stream.clock, finite(word.startMs, 0));
      const endMs = Math.max(startMs, toSessionMs(stream.clock, finite(word.endMs, word.startMs)));
      return {
        text,
        norm: normalize(text),
        startMs,
        endMs,
        confidence: bounded(word.confidence),
      };
    })
    .filter((word) => word.text !== "" && word.norm !== "")
    .sort((a, b) => a.startMs - b.startMs || a.endMs - b.endMs || a.norm.localeCompare(b.norm));
}

function makeStream(stream: StreamTranscript): RoverStream | undefined {
  const words = toSessionTime(stream);
  if (words.length === 0) return undefined;
  const signature = words
    .map(
      (word) =>
        `${word.norm}/${word.text}@${word.startMs.toFixed(3)}:${word.endMs.toFixed(3)}#${word.confidence.toFixed(6)}`,
    )
    .join("|");
  return {
    participantId: stream.participantId,
    quality: streamWeight(stream.quality),
    signature,
    words,
    startMs: words[0]!.startMs,
    endMs: words[words.length - 1]!.endMs,
    anchors: 0,
  };
}

/** Regroupe le chemin N=1 sans modifier son comportement historique. */
function groupSingle(words: readonly TimedWord[], options: ConsensusOptions): CanonicalSegmentDraft[] {
  const segments: CanonicalSegmentDraft[] = [];
  let current: TimedWord[] = [];
  const flush = (): void => {
    if (current.length === 0) return;
    const confidence = current.reduce((sum, word) => sum + word.confidence, 0) / current.length;
    segments.push({
      startMs: Math.round(current[0]!.startMs),
      endMs: Math.round(current[current.length - 1]!.endMs),
      text: current.map((word) => word.text).join(" "),
      consensusScore: confidence,
      isDisputed: confidence < options.disputeThreshold,
      variants: [],
      sources: [] as { by: string; rec: string }[],
      support: [] as { by: string; rec: string; segment: number; word: number; startMs: number; endMs: number }[],
      by: [] as string[],
      rec: [] as string[],
    });
    current = [];
  };
  for (const word of words) {
    const previous = current[current.length - 1];
    if (
      previous !== undefined &&
      (word.startMs - previous.endMs > options.segmentGapMs ||
        word.endMs - current[0]!.startMs > options.maxSegmentMs)
    ) {
      flush();
    }
    current.push(word);
  }
  flush();
  return segments;
}

function median(values: readonly number[]): number {
  const ordered = [...values].sort((a, b) => a - b);
  const middle = Math.floor(ordered.length / 2);
  return ordered.length % 2 === 1
    ? ordered[middle]!
    : (ordered[middle - 1]! + ordered[middle]!) / 2;
}

function uniqueNgrams(words: readonly TimedWord[]): Map<string, number> {
  const occurrences = new Map<string, number[]>();
  for (let size = 2; size <= 4; size += 1) {
    for (let index = 0; index + size <= words.length; index += 1) {
      const gram = words.slice(index, index + size).map((word) => word.norm).join("");
      const midpoint = (words[index]!.startMs + words[index + size - 1]!.endMs) / 2;
      const positions = occurrences.get(gram) ?? [];
      positions.push(midpoint);
      occurrences.set(gram, positions);
    }
  }
  return new Map(
    [...occurrences.entries()]
      .filter(([, positions]) => positions.length === 1)
      .map(([gram, positions]) => [gram, positions[0]!] as const),
  );
}

/** Corrige le résiduel inter-flux après l'application du modèle d'horloge officiel. */
function alignClockToPivot(stream: RoverStream, pivot: RoverStream): void {
  const source = uniqueNgrams(stream.words);
  const target = uniqueNgrams(pivot.words);
  const pairs = [...source.entries()]
    .filter(([gram]) => target.has(gram))
    .map(([gram, x]) => [x, target.get(gram)!] as const)
    .filter((pair, index, all) => all.findIndex((candidate) => candidate[0] === pair[0] && candidate[1] === pair[1]) === index)
    .sort((left, right) => left[0] - right[0] || left[1] - right[1]);
  if (pairs.length === 0) return;

  const slopes: number[] = [];
  for (let left = 0; left < pairs.length; left += 1) {
    for (let right = left + 1; right < pairs.length; right += 1) {
      const dx = pairs[right]![0] - pairs[left]![0];
      if (Math.abs(dx) >= 2_000) slopes.push((pairs[right]![1] - pairs[left]![1]) / dx);
    }
  }
  const slope = Math.max(0.97, Math.min(1.03, slopes.length > 0 ? median(slopes) : 1));
  let inliers = pairs;
  let offset = median(pairs.map(([x, y]) => y - slope * x));
  if (pairs.length >= 3) {
    const residuals = pairs.map(([x, y]) => Math.abs(y - (slope * x + offset)));
    const limit = Math.max(350, median(residuals) * 3);
    inliers = pairs.filter((_, index) => residuals[index]! <= limit);
    if (inliers.length > 0) offset = median(inliers.map(([x, y]) => y - slope * x));
  }
  stream.words = stream.words.map((word) => ({
    ...word,
    startMs: slope * word.startMs + offset,
    endMs: slope * word.endMs + offset,
  }));
  stream.startMs = stream.words[0]!.startMs;
  stream.endMs = stream.words[stream.words.length - 1]!.endMs;
  stream.anchors = inliers.length;
}

function observation(word: TimedWord, stream: RoverStream): Observation {
  return {
    ...word,
    participantId: stream.participantId,
    streamKey: `${stream.participantId} ${stream.signature}`,
    weight: word.confidence * stream.quality,
  };
}

function slotReference(slot: Slot): Observation {
  const groups = new Map<string, Observation[]>();
  for (const item of slot.observations) groups.set(item.norm, [...(groups.get(item.norm) ?? []), item]);
  const bestNorm = [...groups.keys()].sort((left, right) => {
    const leftWeight = groups.get(left)!.reduce((sum, item) => sum + item.weight, 0);
    const rightWeight = groups.get(right)!.reduce((sum, item) => sum + item.weight, 0);
    return rightWeight - leftWeight || left.localeCompare(right);
  })[0]!;
  return [...groups.get(bestNorm)!].sort(
    (left, right) => right.weight - left.weight || left.streamKey.localeCompare(right.streamKey) || left.text.localeCompare(right.text),
  )[0]!;
}

function compatible(slot: Slot, word: TimedWord, maxDeltaMs: number): boolean {
  const reference = slotReference(slot);
  return Math.abs((reference.startMs + reference.endMs - word.startMs - word.endMs) / 2) <= maxDeltaMs;
}

function matchCost(slot: Slot, word: TimedWord, maxDeltaMs: number): number {
  const reference = slotReference(slot);
  const delta = Math.abs((reference.startMs + reference.endMs - word.startMs - word.endMs) / 2);
  return (reference.norm === word.norm ? 0 : 1.15) + Math.min(0.65, (delta / maxDeltaMs) * 0.65);
}

type Operation = readonly ["M" | "D" | "I", number, number];

function alignSmall(
  slots: readonly Slot[],
  words: readonly TimedWord[],
  maxDeltaMs: number,
  ignoreTime: boolean,
  slotBase = 0,
  wordBase = 0,
): Operation[] {
  const rows = slots.length;
  const columns = words.length;
  const costs = Array.from({ length: rows + 1 }, () => Array<number>(columns + 1).fill(Number.POSITIVE_INFINITY));
  const paths = Array.from({ length: rows + 1 }, () => Array<"" | "M" | "D" | "I">(columns + 1).fill(""));
  costs[0]![0] = 0;
  for (let row = 1; row <= rows; row += 1) {
    costs[row]![0] = row;
    paths[row]![0] = "D";
  }
  for (let column = 1; column <= columns; column += 1) {
    costs[0]![column] = column;
    paths[0]![column] = "I";
  }
  const priority = { M: 0, D: 1, I: 2 } as const;
  for (let row = 1; row <= rows; row += 1) {
    for (let column = 1; column <= columns; column += 1) {
      const choices: Array<readonly [number, "M" | "D" | "I"]> = [
        [costs[row - 1]![column]! + 1, "D"],
        [costs[row]![column - 1]! + 1, "I"],
      ];
      if (ignoreTime || compatible(slots[row - 1]!, words[column - 1]!, maxDeltaMs)) {
        choices.push([
          costs[row - 1]![column - 1]! + matchCost(slots[row - 1]!, words[column - 1]!, maxDeltaMs),
          "M",
        ]);
      }
      choices.sort((left, right) =>
        Math.round(left[0] * 1e9) - Math.round(right[0] * 1e9) || priority[left[1]] - priority[right[1]],
      );
      costs[row]![column] = choices[0]![0];
      paths[row]![column] = choices[0]![1];
    }
  }
  const operations: Operation[] = [];
  let row = rows;
  let column = columns;
  while (row > 0 || column > 0) {
    const operation = paths[row]![column]!;
    operations.push([operation as "M" | "D" | "I", slotBase + row - 1, wordBase + column - 1]);
    if (operation === "M") {
      row -= 1;
      column -= 1;
    } else if (operation === "D") {
      row -= 1;
    } else {
      column -= 1;
    }
  }
  return operations.reverse();
}

/** Monotonic unique-token anchors keep long lectures out of a quadratic matrix. */
function lexicalAnchors(slots: readonly Slot[], words: readonly TimedWord[]): Array<readonly [number, number]> {
  const left = new Map<string, number[]>();
  const right = new Map<string, number[]>();
  slots.forEach((slot, index) => {
    const norm = slotReference(slot).norm;
    left.set(norm, [...(left.get(norm) ?? []), index]);
  });
  words.forEach((word, index) => right.set(word.norm, [...(right.get(word.norm) ?? []), index]));
  const candidates = [...left.entries()]
    .filter(([norm, indexes]) => indexes.length === 1 && right.get(norm)?.length === 1)
    .map(([norm, indexes]) => [indexes[0]!, right.get(norm)![0]!] as const)
    .sort((a, b) => a[0] - b[0]);
  const anchors: Array<readonly [number, number]> = [];
  let lastRight = -1;
  for (const candidate of candidates) {
    if (candidate[1] > lastRight) {
      anchors.push(candidate);
      lastRight = candidate[1];
    }
  }
  return anchors;
}

function align(slots: readonly Slot[], words: readonly TimedWord[], options: ConsensusOptions, ignoreTime: boolean): Operation[] {
  const anchors = lexicalAnchors(slots, words);
  const operations: Operation[] = [];
  let slotStart = 0;
  let wordStart = 0;
  for (const [slotIndex, wordIndex] of [...anchors, [slots.length, words.length] as const]) {
    const slotCount = slotIndex - slotStart;
    const wordCount = wordIndex - wordStart;
    if (slotCount * wordCount <= 50_000) {
      operations.push(
        ...alignSmall(
          slots.slice(slotStart, slotIndex),
          words.slice(wordStart, wordIndex),
          options.maxAlignmentDeltaMs,
          ignoreTime,
          slotStart,
          wordStart,
        ),
      );
    } else {
      for (let index = slotStart; index < slotIndex; index += 1) operations.push(["D", index, wordStart - 1]);
      for (let index = wordStart; index < wordIndex; index += 1) operations.push(["I", slotIndex - 1, index]);
    }
    if (slotIndex < slots.length && wordIndex < words.length) operations.push(["M", slotIndex, wordIndex]);
    slotStart = slotIndex + 1;
    wordStart = wordIndex + 1;
  }
  return operations;
}

function addStream(slots: readonly Slot[], stream: RoverStream, options: ConsensusOptions): Slot[] {
  const operations = align(slots, stream.words, options, stream.anchors === 0);
  const out: Slot[] = [];
  for (const [operation, slotIndex, wordIndex] of operations) {
    if (operation === "M") {
      out.push({ observations: [...slots[slotIndex]!.observations, observation(stream.words[wordIndex]!, stream)] });
    } else if (operation === "D") {
      out.push(slots[slotIndex]!);
    } else {
      out.push({ observations: [observation(stream.words[wordIndex]!, stream)] });
    }
  }
  return out;
}

const DELETION = " ";

function vote(slot: Slot, streams: readonly RoverStream[], options: ConsensusOptions): VotedWord {
  const middle = median(slot.observations.map((item) => (item.startMs + item.endMs) / 2));
  const byAlternative = new Map<string, Map<string, Observation>>();
  const present = new Set<string>();
  for (const item of slot.observations) {
    present.add(item.participantId);
    const contributors = byAlternative.get(item.norm) ?? new Map<string, Observation>();
    const current = contributors.get(item.participantId);
    if (current === undefined || item.weight > current.weight || (item.weight === current.weight && item.streamKey < current.streamKey)) {
      contributors.set(item.participantId, item);
    }
    byAlternative.set(item.norm, contributors);
  }
  for (const stream of streams) {
    if (
      !present.has(stream.participantId) &&
      stream.startMs - 250 <= middle &&
      middle <= stream.endMs + 250
    ) {
      const contributors = byAlternative.get(DELETION) ?? new Map<string, Observation>();
      const deleted: Observation = {
        text: "",
        norm: DELETION,
        startMs: middle,
        endMs: middle,
        confidence: 1,
        participantId: stream.participantId,
        streamKey: `${stream.participantId} ${stream.signature}`,
        weight: stream.quality,
      };
      const current = contributors.get(stream.participantId);
      if (current === undefined || deleted.weight > current.weight || (deleted.weight === current.weight && deleted.streamKey < current.streamKey)) {
        contributors.set(stream.participantId, deleted);
      }
      byAlternative.set(DELETION, contributors);
    }
  }
  const alternatives: Alternative[] = [...byAlternative.entries()]
    .map(([norm, contributors]) => {
      const observations = [...contributors.values()].sort(
        (left, right) => right.weight - left.weight || left.streamKey.localeCompare(right.streamKey) || left.text.localeCompare(right.text),
      );
      return {
        norm,
        text: norm === DELETION ? "" : observations[0]!.text,
        total: observations.reduce((sum, item) => sum + item.weight, 0),
        observations,
      };
    })
    .sort((left, right) => right.total - left.total || Number(left.norm === DELETION) - Number(right.norm === DELETION) || left.norm.localeCompare(right.norm));
  const denominator = alternatives.reduce((sum, alternative) => sum + alternative.total, 0) || 1;
  const winner = alternatives[0]!;
  const score = winner.total / denominator;
  const second = alternatives[1]?.total ?? 0;
  const disputed = alternatives.length > 1 && (score < options.disputeThreshold || (winner.total - second) / denominator < options.marginThreshold);
  const winnerWeight = winner.observations.reduce((sum, item) => sum + item.weight, 0) || 1;
  // Compute support and sources for the VotedWord from the winner's observations
  const supportList: { by: string; rec: string; segment: number; word: number; startMs: number; endMs: number }[] = [];
  winner.observations.forEach((obs, index) => {
    supportList.push({
      by: obs.participantId,
      rec: obs.streamKey, // Note: streamKey is participantId+signature, not the recording ID. We don't have the recording ID in the Observation.
      segment: 0, // We don't have the segment index in the Observation.
      word: index, // Use the index in the observations array as the word index.
      startMs: obs.startMs,
      endMs: obs.endMs,
    });
  });

  // Compute unique (by, rec) pairs for sources
  const sourcesSet = new Set<string>();
  supportList.forEach(s => {
    sourcesSet.add(`${s.by}|${s.rec}`);
  });
  const sources: { by: string; rec: string }[] = Array.from(sourcesSet).map(s => {
    const parts = s.split('|');
    const by = parts[0] ?? '';
    const rec = parts[1] ?? '';
    return { by, rec };
  }).sort((a, b) => a.by.localeCompare(b.by) || a.rec.localeCompare(b.rec));

  // Compute unique by and rec
  const bySet = new Set<string>();
  const recSet = new Set<string>();
  supportList.forEach(s => {
    bySet.add(s.by);
    recSet.add(s.rec);
  });
  const by = Array.from(bySet).sort();
  const rec = Array.from(recSet).sort();

  return {
    deleted: winner.norm === DELETION,
    text: winner.text,
    startMs: median(winner.observations.map((item) => item.startMs)),
    endMs: median(winner.observations.map((item) => item.endMs)),
    confidence: winner.observations.reduce((sum, item) => sum + item.confidence * item.weight, 0) / winnerWeight,
    score,
    disputed,
    alternatives,
    sources,
    support: supportList,
    by,
    rec,
  };
}

function renderVariants(word: VotedWord): SegmentVariant[] {
  const total = word.alternatives.reduce((sum, alternative) => sum + alternative.total, 0) || 1;
  return word.alternatives.map((alternative) => ({
    text: alternative.text,
    score: alternative.total / total,
    participantIds: alternative.observations.map((item) => item.participantId).sort(),
  }));
}

function canonicalSegments(voted: readonly VotedWord[], options: ConsensusOptions): CanonicalSegmentDraft[] {
  const kept = voted.filter((word) => !word.deleted);
  if (kept.length === 0) return [];
  const groups: VotedWord[][] = [];
  let current: VotedWord[] = [];
  for (const word of kept) {
    const previous = current[current.length - 1];
    if (
      previous !== undefined &&
      (word.startMs - previous.endMs > options.segmentGapMs ||
        Math.max(previous.endMs, word.endMs) - current[0]!.startMs > options.maxSegmentMs)
    ) {
      groups.push(current);
      current = [];
    }
    current.push(word);
  }
  if (current.length > 0) groups.push(current);

  const segments = groups.map((group) => {
    const durations = group.map((word) => Math.max(0.01, word.endMs - word.startMs));
    const denominator = durations.reduce((sum, duration) => sum + duration, 0);

    // Compute weighted average confidence and consensusScore
    let sumConfidence = 0;
    let sumScore = 0;
    group.forEach((word, index) => {
      const weight = durations[index]!;
      sumConfidence += word.confidence * weight;
      sumScore += word.score * weight;
    });
    const avgConfidence = sumConfidence / denominator;
    const consensusScore = sumScore / denominator;

    // Gather support and compute sources, by, rec from support
    const supportList: { by: string; rec: string; segment: number; word: number; startMs: number; endMs: number }[] = [];
    group.forEach(word => {
      supportList.push(...word.support);
    });

    // Compute unique (by, rec) pairs for sources
    const sourcesSet = new Set<string>();
    supportList.forEach(s => {
      sourcesSet.add(`${s.by}|${s.rec}`);
    });
    const sources: { by: string; rec: string }[] = Array.from(sourcesSet).map(s => {
      const parts = s.split('|');
      const by = parts[0] ?? '';
      const rec = parts[1] ?? '';
      return { by, rec };
    }).sort((a, b) => a.by.localeCompare(b.by) || a.rec.localeCompare(b.rec));

    // Compute unique by and rec
    const bySet = new Set<string>();
    const recSet = new Set<string>();
    supportList.forEach(s => {
      bySet.add(s.by);
      recSet.add(s.rec);
    });
    const by = Array.from(bySet).sort();
    const rec = Array.from(recSet).sort();

    return {
      startMs: Math.round(Math.min(...group.map((word) => word.startMs))),
      endMs: Math.round(Math.max(...group.map((word) => word.endMs))),
      text: group.map((word) => word.text).join(" "),
      avgConfidence,
      consensusScore,
      isDisputed: group.some((word) => word.disputed),
      variants: group.flatMap((word) => (word.disputed ? renderVariants(word) : [])),
      sources,
      support: supportList,
      by: [...new Set(supportList.map((item) => item.by))].sort(),
      rec: [...new Set(supportList.map((item) => item.rec))].sort(),
    } satisfies CanonicalSegmentDraft;
  });

  // Une insertion rejetée est une suppression gagnante : rattacher son désaccord
  // au segment temporel le plus proche afin que l'alternative reste observable.
  for (const word of voted) {
    if (!word.deleted || !word.disputed) continue;
    const middle = (word.startMs + word.endMs) / 2;
    let targetIndex = 0;
    let bestDistance = Number.POSITIVE_INFINITY;
    segments.forEach((segment, index) => {
      const distance = segment.startMs <= middle && middle <= segment.endMs
        ? 0
        : Math.min(Math.abs(middle - segment.startMs), Math.abs(middle - segment.endMs));
      if (distance < bestDistance) {
        bestDistance = distance;
        targetIndex = index;
      }
    });
    const target = segments[targetIndex]!;
    segments[targetIndex] = {
      ...target,
      isDisputed: true,
      variants: [...target.variants, ...renderVariants(word)],
    };
  }
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

  if (streams.length === 1) {
    const segments = groupSingle(toSessionTime(streams[0]!), options);
    return {
      segments,
      stats: {
        mode: "single",
        streamCount: 1,
        segmentCount: segments.length,
        disputedCount: segments.filter((segment) => segment.isDisputed).length,
      },
    };
  }

  const prepared = streams.map(makeStream).filter((stream): stream is RoverStream => stream !== undefined);
  if (prepared.length === 0) {
    return {
      segments: [],
      stats: { mode: "rover", streamCount: streams.length, segmentCount: 0, disputedCount: 0 },
    };
  }
  if (prepared.length === 1) {
    const segments = groupSingle(prepared[0]!.words, options);
    return {
      segments,
      stats: {
        mode: "rover",
        streamCount: streams.length,
        segmentCount: segments.length,
        disputedCount: segments.filter((segment) => segment.isDisputed).length,
      },
    };
  }

  const ordered = [...prepared].sort(
    (left, right) => right.quality - left.quality || right.words.length - left.words.length || left.participantId.localeCompare(right.participantId) || left.signature.localeCompare(right.signature),
  );
  const pivot = ordered[0]!;
  for (const stream of ordered.slice(1)) alignClockToPivot(stream, pivot);
  let slots: Slot[] = pivot.words.map((word) => ({ observations: [observation(word, pivot)] }));
  for (const stream of ordered.slice(1)) slots = addStream(slots, stream, options);

  const voted = slots.map((slot) => vote(slot, ordered, options));
  const segments = canonicalSegments(voted, options);
  return {
    segments,
    stats: {
      mode: "rover",
      streamCount: streams.length,
      segmentCount: segments.length,
      disputedCount: voted.filter((word) => word.disputed).length,
    },
  };
}
