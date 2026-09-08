import { IDENTITY_CLOCK } from "@amphi/shared";
import { describe, expect, it } from "vitest";
import { merge, streamWeight, type StreamTranscript, type Word } from "./merge.js";
import { normalizeForWer, wer } from "./wer.js";

const goodQuality = { snr: 24, meanConfidence: 0.9, gapRatio: 0 };
const poorQuality = { snr: 6, meanConfidence: 0.45, gapRatio: 0.2 };

function words(spec: readonly [string, number, number, number][]): Word[] {
  return spec.map(([text, startMs, endMs, confidence]) => ({ text, startMs, endMs, confidence }));
}

function stream(
  participantId: string,
  quality: StreamTranscript["quality"],
  ws: Word[],
  clock = IDENTITY_CLOCK,
): StreamTranscript {
  return { participantId, quality, clock, words: ws };
}

describe("merge — flux unique", () => {
  it("coupe les segments aux silences", () => {
    const result = merge([
      stream(
        "p1",
        goodQuality,
        words([
          ["la", 0, 200, 0.9],
          ["régularisation", 210, 900, 0.92],
          // Silence d'une seconde : nouveau segment.
          ["ridge", 2000, 2400, 0.88],
          ["pénalise", 2410, 3000, 0.9],
        ]),
      ),
    ]);

    expect(result.stats.mode).toBe("single");
    expect(result.segments).toHaveLength(2);
    expect(result.segments[0]?.text).toBe("la régularisation");
    expect(result.segments[1]?.text).toBe("ridge pénalise");
  });

  it("borne la durée d'un segment pour que les ancres restent utiles", () => {
    // Un locuteur qui ne respire pas produirait sinon une ancre de trois minutes,
    // qui ne renvoie nulle part de précis quand on clique dessus.
    const long = words(
      Array.from({ length: 200 }, (_, i): [string, number, number, number] => [
        `mot${i}`,
        i * 300,
        i * 300 + 250,
        0.9,
      ]),
    );
    const result = merge([stream("p1", goodQuality, long)]);
    for (const segment of result.segments) {
      expect(segment.endMs - segment.startMs).toBeLessThanOrEqual(15_000);
    }
    expect(result.segments.length).toBeGreaterThan(1);
  });

  it("marque disputé un segment de faible confiance", () => {
    const result = merge([
      stream(
        "p1",
        poorQuality,
        words([
          ["hétéroscédasticité", 0, 900, 0.3],
          ["peut-être", 910, 1400, 0.35],
        ]),
      ),
    ]);
    expect(result.segments[0]?.isDisputed).toBe(true);
    expect(result.stats.disputedCount).toBe(1);
  });

  it("convertit les timestamps en temps de session via l'horloge du participant", () => {
    // Le flux est en retard de 1 200 ms et son quartz avance de 100 ppm.
    const clock = { a: 1.0001, b: 1_200 };
    const result = merge([
      stream("p1", goodQuality, words([["bonjour", 10_000, 10_500, 0.9]]), clock),
    ]);
    expect(result.segments[0]?.startMs).toBe(11_201);
  });

  it("ignore les mots vides sans casser la segmentation", () => {
    const result = merge([
      stream(
        "p1",
        goodQuality,
        words([
          ["  ", 0, 50, 0.1],
          ["donc", 60, 300, 0.9],
        ]),
      ),
    ]);
    expect(result.segments[0]?.text).toBe("donc");
  });

  it("renvoie un résultat vide plutôt que de planter sans flux", () => {
    expect(merge([]).segments).toHaveLength(0);
  });
});

describe("merge — plusieurs flux (avant ROVER, M3)", () => {
  it("retient le flux de meilleure qualité et le déclare franchement", () => {
    // Ce test documente l'état actuel. Quand ROVER arrivera en M3, `mode` devra
    // passer à "rover" et ce test devra être réécrit — c'est le but.
    const result = merge([
      stream("faible", poorQuality, words([["ridge", 0, 400, 0.4]])),
      stream("bon", goodQuality, words([["ridge", 0, 400, 0.95]])),
    ]);
    expect(result.stats.mode).toBe("best-stream");
    expect(result.stats.streamCount).toBe(2);
    expect(result.segments[0]?.consensusScore).toBeCloseTo(0.95, 2);
  });

  it("classe les flux par poids composite, pas par confiance seule", () => {
    // Un flux très confiant mais très bruité ne doit pas primer : une ASR sûre
    // d'elle sur un signal pourri est exactement le cas dangereux.
    const confident = { snr: 3, meanConfidence: 0.95, gapRatio: 0.4 };
    expect(streamWeight(goodQuality)).toBeGreaterThan(streamWeight(confident));
  });
});

describe("wer", () => {
  // Chaque type d'erreur est isolé : un exemple qui les mélange a souvent
  // plusieurs chemins d'édition de coût minimal, et la répartition devient
  // arbitraire même si le WER, lui, reste juste.
  it("compte une substitution", () => {
    const result = wer("le chat dort sur le tapis", "le chien dort sur le tapis");
    expect(result).toMatchObject({ substitutions: 1, deletions: 0, insertions: 0 });
    expect(result.wer).toBeCloseTo(1 / 6, 6);
  });

  it("compte une omission", () => {
    const result = wer("le chat dort sur le tapis", "le chat dort le tapis");
    expect(result).toMatchObject({ substitutions: 0, deletions: 1, insertions: 0 });
  });

  it("compte une insertion", () => {
    const result = wer("le chat dort", "le chat dort profondément");
    expect(result).toMatchObject({ substitutions: 0, deletions: 0, insertions: 1 });
    expect(result.referenceWords).toBe(3);
  });

  it("totalise correctement quand les trois se combinent", () => {
    const result = wer("le chat dort sur le tapis", "le chien dort le grand tapis");
    const total = result.substitutions + result.deletions + result.insertions;
    expect(total).toBe(3);
    expect(result.wer).toBeCloseTo(3 / 6, 6);
  });

  it("ignore la casse, les accents et la ponctuation", () => {
    expect(wer("Hétéroscédasticité, oui !", "heteroscedasticite oui").wer).toBe(0);
  });

  it("donne 0 sur une transcription parfaite et 1 sur une transcription vide", () => {
    expect(wer("gradient boosting", "gradient boosting").wer).toBe(0);
    expect(wer("gradient boosting", "").wer).toBe(1);
  });

  it("normalise les chiffres et le jargon sans les détruire", () => {
    expect(normalizeForWer("L2 — la norme L1 !")).toEqual(["l2", "la", "norme", "l1"]);
  });
});
