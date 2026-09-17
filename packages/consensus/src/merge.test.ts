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
    expect(result.segments[0]?.sources).toEqual(expect.any(Array));
    expect(result.segments[0]?.support).toEqual(expect.any(Array));
    expect(result.segments[0]?.by).toEqual(expect.any(Array));
    expect(result.segments[0]?.rec).toEqual(expect.any(Array));
    expect(result.segments[1]?.text).toBe("ridge pénalise");
    expect(result.segments[1]?.sources).toEqual(expect.any(Array));
    expect(result.segments[1]?.support).toEqual(expect.any(Array));
    expect(result.segments[1]?.by).toEqual(expect.any(Array));
    expect(result.segments[1]?.rec).toEqual(expect.any(Array));
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
      expect(segment.sources).toEqual(expect.any(Array));
      expect(segment.support).toEqual(expect.any(Array));
      expect(segment.by).toEqual(expect.any(Array));
      expect(segment.rec).toEqual(expect.any(Array));
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
    expect(result.segments[0]?.sources).toEqual(expect.any(Array));
    expect(result.segments[0]?.support).toEqual(expect.any(Array));
    expect(result.segments[0]?.by).toEqual(expect.any(Array));
    expect(result.segments[0]?.rec).toEqual(expect.any(Array));
  });

  it("convertit les timestamps en temps de session via l'horloge du participant", () => {
    // Le flux est en retard de 1 200 ms et son quartz avance de 100 ppm.
    const clock = { a: 1.0001, b: 1_200 };
    const result = merge([
      stream("p1", goodQuality, words([["bonjour", 10_000, 10_500, 0.9]]), clock),
    ]);
    expect(result.segments[0]?.startMs).toBe(11_201);
    expect(result.segments[0]?.sources).toEqual(expect.any(Array));
    expect(result.segments[0]?.support).toEqual(expect.any(Array));
    expect(result.segments[0]?.by).toEqual(expect.any(Array));
    expect(result.segments[0]?.rec).toEqual(expect.any(Array));
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
    expect(result.segments[0]?.sources).toEqual(expect.any(Array));
    expect(result.segments[0]?.support).toEqual(expect.any(Array));
    expect(result.segments[0]?.by).toEqual(expect.any(Array));
    expect(result.segments[0]?.rec).toEqual(expect.any(Array));
  });

  it("renvoie un résultat vide plutôt que de planter sans flux", () => {
    expect(merge([]).segments).toHaveLength(0);
  });
});

describe("merge — ROVER multi-flux", () => {
  const phrase = (middle: string, confidence = 0.9): Word[] =>
    words([
      ["le", 0, 200, confidence],
      [middle, 250, 650, confidence],
      ["converge", 700, 1100, confidence],
    ]);

  it("vote réellement entre les flux au lieu de retenir le meilleur", () => {
    const result = merge([
      stream("p1", goodQuality, phrase("gradient")),
      stream("p2", goodQuality, phrase("gradient")),
      stream("p3", goodQuality, phrase("gradiant")),
    ]);

    expect(result.stats).toMatchObject({ mode: "rover", streamCount: 3 });
    expect(result.segments[0]?.text).toBe("le gradient converge");
    expect(result.segments[0]?.consensusScore).toBeGreaterThan(0.65);
    expect(result.segments[0]?.sources).toEqual(expect.any(Array));
    expect(result.segments[0]?.support).toEqual(expect.any(Array));
    expect(result.segments[0]?.by).toEqual(expect.any(Array));
    expect(result.segments[0]?.rec).toEqual(expect.any(Array));
  });

  it("applique t_session = a*t_local+b pour compenser offset et dérive", () => {
    const reference = words([
      ["un", 10_000, 10_200, 0.9],
      ["modèle", 14_000, 14_400, 0.9],
      ["linéaire", 18_000, 18_500, 0.9],
    ]);
    // Ces temps locaux deviennent exactement ceux de reference avec a=1.01, b=750.
    const drifting = reference.map((word) => ({
      ...word,
      startMs: (word.startMs - 750) / 1.01,
      endMs: (word.endMs - 750) / 1.01,
    }));
    const result = merge([
      stream("reference", goodQuality, reference),
      stream("mobile", goodQuality, drifting, { a: 1.01, b: 750 }),
    ]);

    expect(result.segments.map((segment) => segment.text).join(" ")).toBe("un modèle linéaire");
    expect(result.segments[0]?.startMs).toBe(10_000);
    expect(result.segments.every((segment) => !segment.isDisputed)).toBe(true);
    // Check new fields for all segments
    for (const segment of result.segments) {
      expect(segment.sources).toEqual(expect.any(Array));
      expect(segment.support).toEqual(expect.any(Array));
      expect(segment.by).toEqual(expect.any(Array));
      expect(segment.rec).toEqual(expect.any(Array));
    }
  });

  it("résout une substitution majoritaire et expose le désaccord à égalité", () => {
    const majority = merge([
      stream("p1", goodQuality, phrase("chat")),
      stream("p2", goodQuality, phrase("chat")),
      stream("p3", goodQuality, phrase("chien")),
    ]);
    expect(majority.segments[0]?.text).toBe("le chat converge");
    expect(majority.segments[0]?.sources).toEqual(expect.any(Array));
    expect(majority.segments[0]?.support).toEqual(expect.any(Array));
    expect(majority.segments[0]?.by).toEqual(expect.any(Array));
    expect(majority.segments[0]?.rec).toEqual(expect.any(Array));

    const tie = merge([
      stream("alice", goodQuality, phrase("chat")),
      stream("bob", goodQuality, phrase("chien")),
    ]);
    const disputed = tie.segments[0];
    expect(disputed?.isDisputed).toBe(true);
    expect(disputed?.variants.map((variant) => variant.text)).toEqual(["chat", "chien"]);
    expect(disputed?.variants.map((variant) => variant.participantIds)).toEqual([["alice"], ["bob"]]);
    expect(tie.stats.disputedCount).toBe(1);
    expect(disputed?.sources).toEqual(expect.any(Array));
    expect(disputed?.support).toEqual(expect.any(Array));
    expect(disputed?.by).toEqual(expect.any(Array));
    expect(disputed?.rec).toEqual(expect.any(Array));
  });

  it("rejette une insertion minoritaire mais la conserve comme alternative", () => {
    const ordinary = phrase("gradient");
    const inserted = words([
      ["le", 0, 200, 0.9],
      ["grand", 205, 245, 0.99],
      ["gradient", 250, 650, 0.9],
      ["converge", 700, 1100, 0.9],
    ]);
    const result = merge([
      stream("p1", goodQuality, ordinary),
      stream("p2", goodQuality, ordinary),
      stream("p3", goodQuality, inserted),
    ], { disputeThreshold: 0.67 });

    expect(result.segments[0]?.text).toBe("le gradient converge");
    expect(result.segments[0]?.isDisputed).toBe(true);
    expect(result.segments[0]?.variants.some((variant) => variant.text === "grand")).toBe(true);
    expect(result.segments[0]?.variants.some((variant) => variant.text === "")).toBe(true);
    expect(result.segments[0]?.sources).toEqual(expect.any(Array));
    expect(result.segments[0]?.support).toEqual(expect.any(Array));
    expect(result.segments[0]?.by).toEqual(expect.any(Array));
    expect(result.segments[0]?.rec).toEqual(expect.any(Array));
  });

  it("survit à une suppression minoritaire", () => {
    const omitted = words([
      ["le", 0, 200, 0.9],
      ["converge", 700, 1100, 0.9],
    ]);
    const result = merge([
      stream("p1", goodQuality, phrase("gradient")),
      stream("p2", goodQuality, phrase("gradient")),
      stream("p3", goodQuality, omitted),
    ]);

    expect(result.segments[0]?.text).toBe("le gradient converge");
    expect(result.segments[0]?.sources).toEqual(expect.any(Array));
    expect(result.segments[0]?.support).toEqual(expect.any(Array));
    expect(result.segments[0]?.by).toEqual(expect.any(Array));
    expect(result.segments[0]?.rec).toEqual(expect.any(Array));
  });

  it("plafonne à un vote par contributeur et variante", () => {
    const result = merge([
      stream("alice", goodQuality, phrase("faux", 0.6)),
      stream("alice", goodQuality, phrase("faux", 0.6)),
      stream("bob", goodQuality, phrase("juste", 0.95)),
    ], { disputeThreshold: 0.7 });

    expect(result.segments[0]?.text).toBe("le juste converge");
    const wrong = result.segments[0]?.variants.find((variant) => variant.text === "faux");
    expect(wrong?.participantIds).toEqual(["alice"]);
    expect(result.segments[0]?.sources).toEqual(expect.any(Array));
    expect(result.segments[0]?.support).toEqual(expect.any(Array));
    expect(result.segments[0]?.by).toEqual(expect.any(Array));
    expect(result.segments[0]?.rec).toEqual(expect.any(Array));
  });

  it("reste déterministe quand l'ordre des flux change", () => {
    const inputs = [
      stream("z", goodQuality, phrase("bêta")),
      stream("a", goodQuality, phrase("alpha")),
      stream("m", poorQuality, phrase("gamma")),
    ];
    const forward = merge(inputs);
    const reverse = merge([...inputs].reverse());
    const rotated = merge([inputs[1]!, inputs[2]!, inputs[0]!]);

    expect(reverse).toEqual(forward);
    expect(rotated).toEqual(forward);
    // Check new fields for the forward result
    for (const segment of forward.segments) {
      expect(segment.sources).toEqual(expect.any(Array));
      expect(segment.support).toEqual(expect.any(Array));
      expect(segment.by).toEqual(expect.any(Array));
      expect(segment.rec).toEqual(expect.any(Array));
    }
  });

  it("préserve le format et le comportement historique à un seul flux", () => {
    const result = merge([stream("legacy", goodQuality, phrase("gradient"))]);
    expect(result).toEqual({
      segments: [{
        startMs: 0,
        endMs: 1100,
        text: "le gradient converge",
        consensusScore: 0.9,
        isDisputed: false,
        variants: [],
        sources: expect.any(Array),
        support: expect.any(Array),
        by: expect.any(Array),
        rec: expect.any(Array),
      }],
      stats: { mode: "single", streamCount: 1, segmentCount: 1, disputedCount: 0 },
    });
  });

  it("classe toujours les flux par poids composite, pas par confiance seule", () => {
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
