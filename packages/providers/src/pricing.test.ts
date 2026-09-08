import { describe, expect, it } from "vitest";
import { asrCostMilliCents, formatEuros, llmCostMilliCents } from "./pricing.js";

/**
 * Ces tests verrouillent le modèle de coût publié au §11 de l'ARCHITECTURE.
 * Si quelqu'un change un tarif sans mettre le document à jour, c'est ici que ça
 * casse — et c'est voulu : un budget qui dérive en silence est un budget faux.
 */

/** Un cours d'une heure : ~18 000 tokens en entrée, ~5 000 en sortie (§11.3). */
const DOC_INPUT_TOKENS = 18_000;
const DOC_OUTPUT_TOKENS = 5_000;

/** 30 h de cours par semaine ≈ 130 h par mois (§1). */
const HOURS_PER_MONTH = 130;

describe("coût ASR", () => {
  it("le moteur local est gratuit — c'est tout l'intérêt de l'ADR-15", () => {
    expect(asrCostMilliCents("mlx-local", 3600)).toBe(0);
  });

  it("Groq revient à 0,037 € par heure d'audio", () => {
    const perHour = asrCostMilliCents("groq", 3600) / 1000;
    expect(perHour).toBeCloseTo(0.037, 3);
  });

  it("OVH revient à 0,046 € par heure, soit le prix du profil 100 % UE", () => {
    const perHour = asrCostMilliCents("ovh", 3600) / 1000;
    expect(perHour).toBeCloseTo(0.046, 3);
  });

  it("applique le minimum de facturation de 10 s de Groq", () => {
    // Nos chunks font 20 à 30 s, donc ce minimum ne coûte rien en pratique —
    // mais il mordrait si on découpait plus fin, ce qui est bon à savoir avant
    // de « optimiser » la taille des chunks un jour.
    expect(asrCostMilliCents("groq", 4)).toBe(asrCostMilliCents("groq", 10));
    expect(asrCostMilliCents("groq", 25)).toBeGreaterThan(asrCostMilliCents("groq", 10));
  });

  it("le repli payant sur un mois entier reste sous le pire cas annoncé", () => {
    // §11.4 : « pire cas — Mac jamais réveillé du mois » = 7,14 €, dont 4,81 €
    // d'ASR à un seul flux.
    const asrEuros = (asrCostMilliCents("groq", 3600) * HOURS_PER_MONTH) / 1000;
    expect(asrEuros).toBeCloseTo(4.81, 1);
  });
});

describe("coût LLM par document de cours", () => {
  const cost = (key: Parameters<typeof llmCostMilliCents>[0]): number =>
    llmCostMilliCents(key, DOC_INPUT_TOKENS, DOC_OUTPUT_TOKENS) / 1000;

  it("respecte le tableau du §11.3", () => {
    expect(cost("mlx-local")).toBe(0);
    expect(cost("mistral-small")).toBeCloseTo(0.0052, 4);
    expect(cost("claude-haiku")).toBeCloseTo(0.04, 3);
    expect(cost("claude-sonnet")).toBeCloseTo(0.119, 3);
    expect(cost("claude-opus")).toBeCloseTo(0.198, 3);
  });

  it("Mistral Small tient le budget mensuel du document final", () => {
    expect(cost("mistral-small") * HOURS_PER_MONTH).toBeCloseTo(0.68, 2);
  });

  it("Haiku coûterait ~7,5 fois plus pour le même travail", () => {
    // Le rapport qui justifie ADR-16. Il ne dit rien de la qualité — c'est le
    // test à l'aveugle de M1 qui tranchera, pas ce ratio.
    expect(cost("claude-haiku") / cost("mistral-small")).toBeCloseTo(7.54, 2);
  });

  it("le budget nominal complet reste sous 3 € par mois", () => {
    // §11.4 : 2,77 €/mois — repli ASR sur ~20 % des heures, document Mistral.
    const fallbackAsr = (asrCostMilliCents("groq", 3600) * HOURS_PER_MONTH * 0.2) / 1000;
    const documents = cost("mistral-small") * HOURS_PER_MONTH;
    const domain = 1;
    expect(fallbackAsr + documents + domain).toBeLessThan(3);
  });
});

describe("formatEuros", () => {
  it("garde quatre décimales sur les petits montants, deux sur les gros", () => {
    expect(formatEuros(5.244)).toBe("0.0052 €");
    expect(formatEuros(2770)).toBe("2.77 €");
  });
});
