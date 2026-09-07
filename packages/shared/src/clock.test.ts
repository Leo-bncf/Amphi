import { describe, expect, it } from "vitest";
import {
  MAX_CLOCK_RESIDUAL_MS,
  driftOverMs,
  driftPpm,
  estimateOffsetMs,
  fromSessionMs,
  toSessionMs,
  type RoundTripSample,
} from "./clock.js";

/**
 * Construit un aller-retour à partir d'une vérité terrain : décalage réel du
 * client, et latence de chaque sens. C'est l'asymétrie qui biaise l'estimation,
 * pas la latence en elle-même.
 */
function roundTrip(trueOffsetMs: number, upMs: number, downMs: number, at = 1_000): RoundTripSample {
  const clientSendMs = at;
  const serverRecvMs = at + trueOffsetMs + upMs;
  const serverSendMs = serverRecvMs + 2; // temps de traitement serveur
  const clientRecvMs = serverSendMs - trueOffsetMs + downMs;
  return { clientSendMs, serverRecvMs, serverSendMs, clientRecvMs };
}

describe("modèle d'horloge affine", () => {
  it("convertit dans les deux sens sans perte", () => {
    const clock = { a: 1.000_02, b: -137 };
    const local = 1_234_567;
    expect(fromSessionMs(clock, toSessionMs(clock, local))).toBeCloseTo(local, 6);
  });

  it("exprime la dérive en ppm", () => {
    expect(driftPpm({ a: 1.000_05, b: 0 })).toBeCloseTo(50, 6);
    expect(driftPpm({ a: 1, b: 0 })).toBe(0);
  });

  /**
   * Le calcul qui justifie l'ADR-03 : sur une heure, un quartz à 100 ppm dérive de
   * 360 ms, soit près du double du budget d'erreur total. Corriger uniquement le
   * décalage initial ne peut donc pas tenir le critère d'acceptation n°3.
   */
  it("montre que la dérive seule dépasse le budget d'erreur sur une heure", () => {
    const oneHourMs = 60 * 60 * 1000;
    expect(driftOverMs({ a: 1.000_1, b: 0 }, oneHourMs)).toBeCloseTo(360, 6);
    expect(Math.abs(driftOverMs({ a: 1.000_1, b: 0 }, oneHourMs))).toBeGreaterThan(MAX_CLOCK_RESIDUAL_MS);

    // Même un quartz honnête à 10 ppm consomme un cinquième du budget.
    expect(driftOverMs({ a: 1.000_01, b: 0 }, oneHourMs)).toBeCloseTo(36, 6);
  });
});

describe("estimateOffsetMs", () => {
  it("retrouve exactement le décalage quand la latence est symétrique", () => {
    const samples = [10, 20, 30, 40, 50].map((l) => roundTrip(500, l, l));
    expect(estimateOffsetMs(samples).offsetMs).toBeCloseTo(500, 6);
  });

  it("rejette les échantillons à fort RTT, qui sont ceux qui mentent", () => {
    // Trois mesures propres, quatre polluées par un uplink saturé. L'asymétrie
    // réelle est unidirectionnelle — c'est la montée qui souffre en 4G — donc
    // les erreurs ne se compensent pas : elles tirent toutes du même côté.
    const samples: RoundTripSample[] = [
      roundTrip(500, 8, 9),
      roundTrip(500, 10, 11),
      roundTrip(500, 9, 12),
      roundTrip(500, 400, 5),
      roundTrip(500, 380, 8),
      roundTrip(500, 350, 10),
      roundTrip(500, 420, 12),
    ];

    const estimate = estimateOffsetMs(samples);
    expect(Math.abs(estimate.offsetMs - 500)).toBeLessThan(5);
    expect(estimate.usedSamples).toBe(3);

    // Une médiane naïve sur tout l'échantillon se serait laissée entraîner.
    const naive = [...samples]
      .map((s) => (s.serverRecvMs - s.clientSendMs + (s.serverSendMs - s.clientRecvMs)) / 2)
      .sort((x, y) => x - y)[3] as number;
    expect(Math.abs(naive - 500)).toBeGreaterThan(Math.abs(estimate.offsetMs - 500));
  });

  it("reste sous le budget de 200 ms sur une 4G plausible", () => {
    const jitter = [45, 60, 38, 120, 52, 200, 41];
    const samples = jitter.map((l, i) => roundTrip(-1_250, l, l + (i % 3) * 12));
    expect(Math.abs(estimateOffsetMs(samples).offsetMs - -1_250)).toBeLessThan(MAX_CLOCK_RESIDUAL_MS);
  });

  it("fonctionne avec un seul échantillon, sans prétendre à la précision", () => {
    const estimate = estimateOffsetMs([roundTrip(300, 20, 20)]);
    expect(estimate.offsetMs).toBeCloseTo(300, 6);
    expect(estimate.usedSamples).toBe(1);
  });

  it("refuse une liste vide plutôt que de renvoyer zéro", () => {
    expect(() => estimateOffsetMs([])).toThrow(/au moins un échantillon/);
  });
});
