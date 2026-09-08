import type { ParticipantId } from "@amphi/shared";
import { describe, expect, it } from "vitest";
import { MAX_LOCAL_STREAMS, planTranscription, type StreamCandidate } from "./routing.js";

const pid = (n: number): ParticipantId =>
  `00000000-0000-4000-8000-${String(n).padStart(12, "0")}` as ParticipantId;

const stream = (n: number, qualityScore: number | null, hasRecentGap = false): StreamCandidate => ({
  participantId: pid(n),
  qualityScore,
  hasRecentGap,
});

describe("planTranscription — moteur local disponible", () => {
  it("transcrit tous les flux, car le local est gratuit", () => {
    const plan = planTranscription({
      candidates: [stream(1, 0.9), stream(2, 0.7), stream(3, 0.6), stream(4, 0.4)],
      profile: "fusion",
      localAvailable: true,
      budgetExhausted: false,
    });

    expect(plan.kind).toBe("transcribe");
    if (plan.kind !== "transcribe") return;
    expect(plan.provider).toBe("mlx-local");
    expect(plan.participantIds).toHaveLength(4);
    expect(plan.degraded).toBe(false);
  });

  it("rend la fusion possible même en profil minimal", () => {
    // C'est le point de l'ADR-15 : l'ASR local supprime la raison de se limiter
    // à un flux, donc le profil de coût cesse de brider la qualité technique.
    const plan = planTranscription({
      candidates: [stream(1, 0.8), stream(2, 0.7), stream(3, 0.6)],
      profile: "minimal",
      localAvailable: true,
      budgetExhausted: false,
    });

    if (plan.kind !== "transcribe") throw new Error("attendu: transcribe");
    expect(plan.participantIds).toHaveLength(3);
  });

  it("plafonne le nombre de flux même en local", () => {
    const many = Array.from({ length: 12 }, (_, i) => stream(i + 1, 0.9 - i * 0.05));
    const plan = planTranscription({
      candidates: many,
      profile: "fusion",
      localAvailable: true,
      budgetExhausted: false,
    });

    if (plan.kind !== "transcribe") throw new Error("attendu: transcribe");
    expect(plan.participantIds).toHaveLength(MAX_LOCAL_STREAMS);
  });

  it("classe par qualité décroissante", () => {
    const plan = planTranscription({
      candidates: [stream(1, 0.2), stream(2, 0.95), stream(3, 0.6)],
      profile: "fusion",
      localAvailable: true,
      budgetExhausted: false,
    });

    if (plan.kind !== "transcribe") throw new Error("attendu: transcribe");
    expect(plan.participantIds).toEqual([pid(2), pid(3), pid(1)]);
  });

  it("relègue un flux troué derrière un flux intact de moindre qualité", () => {
    // Un trou décale l'alignement de tout le réseau de confusion : c'est pire
    // qu'un flux simplement bruité.
    const plan = planTranscription({
      candidates: [stream(1, 0.95, true), stream(2, 0.3, false)],
      profile: "minimal",
      localAvailable: true,
      budgetExhausted: false,
    });

    if (plan.kind !== "transcribe") throw new Error("attendu: transcribe");
    expect(plan.participantIds[0]).toBe(pid(2));
  });
});

describe("planTranscription — repli payant", () => {
  it("dégrade le nombre de flux en même temps que le fournisseur", () => {
    // La règle qui borne le pire cas à 7,14 €/mois. Sans elle, un mois sans le
    // Mac coûterait trois fois plus cher en gardant K = 3 chez Groq.
    const plan = planTranscription({
      candidates: [stream(1, 0.9), stream(2, 0.8), stream(3, 0.7)],
      profile: "fusion",
      localAvailable: false,
      budgetExhausted: false,
    });

    if (plan.kind !== "transcribe") throw new Error("attendu: transcribe");
    expect(plan.provider).toBe("groq");
    expect(plan.participantIds).toEqual([pid(1)]);
    expect(plan.degraded).toBe(true);
    expect(plan.reason).toMatch(/fusion reportée/);
  });

  it("ne se déclare pas dégradé quand le profil ne demandait qu'un flux", () => {
    const plan = planTranscription({
      candidates: [stream(1, 0.9), stream(2, 0.8)],
      profile: "minimal",
      localAvailable: false,
      budgetExhausted: false,
    });

    if (plan.kind !== "transcribe") throw new Error("attendu: transcribe");
    expect(plan.degraded).toBe(false);
  });

  it("diffère plutôt que de dépenser quand le plafond est atteint", () => {
    const plan = planTranscription({
      candidates: [stream(1, 0.9)],
      profile: "fusion",
      localAvailable: false,
      budgetExhausted: true,
    });

    expect(plan.kind).toBe("defer");
    if (plan.kind !== "defer") return;
    expect(plan.reason).toMatch(/plafond mensuel/);
  });

  it("ignore le plafond quand le moteur local est là, puisque ça ne coûte rien", () => {
    const plan = planTranscription({
      candidates: [stream(1, 0.9), stream(2, 0.8)],
      profile: "fusion",
      localAvailable: true,
      budgetExhausted: true,
    });

    if (plan.kind !== "transcribe") throw new Error("attendu: transcribe");
    expect(plan.provider).toBe("mlx-local");
    expect(plan.participantIds).toHaveLength(2);
  });
});

describe("planTranscription — cas limites", () => {
  it("diffère quand il n'y a aucun candidat", () => {
    const plan = planTranscription({
      candidates: [],
      profile: "fusion",
      localAvailable: true,
      budgetExhausted: false,
    });
    expect(plan.kind).toBe("defer");
  });

  it("traite un score manquant comme une qualité moyenne, sans exclure le flux", () => {
    const plan = planTranscription({
      candidates: [stream(1, null), stream(2, 0.9)],
      profile: "minimal",
      localAvailable: false,
      budgetExhausted: false,
    });

    if (plan.kind !== "transcribe") throw new Error("attendu: transcribe");
    expect(plan.participantIds).toEqual([pid(2)]);
  });
});
