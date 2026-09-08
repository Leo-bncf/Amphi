/**
 * Tarifs vérifiés le 2026-09-08 — §11.3 de l'ARCHITECTURE.
 *
 * Tout est exprimé en **millièmes d'euro** (milli-cents) et manipulé en entiers :
 * on ne stocke jamais de la monnaie en flottant. Un centime = 10 milli-cents.
 *
 * Ces valeurs alimentent le `costLedger`, qui est ce qui rend le budget pilotable
 * autrement qu'en découvrant la facture à la fin du mois.
 */

/** Taux de conversion retenu pour les tarifs libellés en dollars. */
export const USD_TO_EUR = 0.92;

export interface AsrPricing {
  readonly provider: string;
  readonly model: string;
  /** Millièmes d'euro par seconde d'audio. */
  readonly milliCentsPerAudioSecond: number;
  /** Certains fournisseurs facturent un minimum par requête (Groq : 10 s). */
  readonly minimumBilledSeconds: number;
}

export const ASR_PRICING = {
  "mlx-local": {
    provider: "mlx-local",
    model: "whisper-large-v3-turbo",
    milliCentsPerAudioSecond: 0,
    minimumBilledSeconds: 0,
  },
  groq: {
    provider: "groq",
    model: "whisper-large-v3-turbo",
    // 0,04 $/h → 0,037 €/h → 0,0102 milli-cents par seconde.
    milliCentsPerAudioSecond: (0.04 * USD_TO_EUR * 1000) / 3600,
    minimumBilledSeconds: 10,
  },
  ovh: {
    provider: "ovh",
    model: "whisper-large-v3-turbo",
    // Tarif affiché au catalogue : 0,00001278 €/s.
    milliCentsPerAudioSecond: 0.00001278 * 1000,
    minimumBilledSeconds: 0,
  },
} as const satisfies Record<string, AsrPricing>;

export type AsrProviderKey = keyof typeof ASR_PRICING;

export interface LlmPricing {
  readonly provider: string;
  readonly model: string;
  readonly milliCentsPerInputToken: number;
  readonly milliCentsPerOutputToken: number;
}

function perMillionUsd(usdPerMillion: number): number {
  return (usdPerMillion * USD_TO_EUR * 1000) / 1_000_000;
}

export const LLM_PRICING = {
  "mlx-local": {
    provider: "mlx-local",
    model: "local",
    milliCentsPerInputToken: 0,
    milliCentsPerOutputToken: 0,
  },
  "mistral-small": {
    provider: "mistral",
    model: "mistral-small-latest",
    milliCentsPerInputToken: perMillionUsd(0.15),
    milliCentsPerOutputToken: perMillionUsd(0.6),
  },
  "claude-haiku": {
    provider: "anthropic",
    model: "claude-haiku-4-5",
    milliCentsPerInputToken: perMillionUsd(1),
    milliCentsPerOutputToken: perMillionUsd(5),
  },
  "claude-sonnet": {
    provider: "anthropic",
    model: "claude-sonnet-5",
    milliCentsPerInputToken: perMillionUsd(3),
    milliCentsPerOutputToken: perMillionUsd(15),
  },
  "claude-opus": {
    provider: "anthropic",
    model: "claude-opus-5",
    milliCentsPerInputToken: perMillionUsd(5),
    milliCentsPerOutputToken: perMillionUsd(25),
  },
} as const satisfies Record<string, LlmPricing>;

export type LlmProviderKey = keyof typeof LLM_PRICING;

export function asrCostMilliCents(key: AsrProviderKey, audioSeconds: number): number {
  const pricing = ASR_PRICING[key];
  const billed = Math.max(audioSeconds, pricing.minimumBilledSeconds);
  return billed * pricing.milliCentsPerAudioSecond;
}

export function llmCostMilliCents(
  key: LlmProviderKey,
  inputTokens: number,
  outputTokens: number,
): number {
  const pricing = LLM_PRICING[key];
  return (
    inputTokens * pricing.milliCentsPerInputToken + outputTokens * pricing.milliCentsPerOutputToken
  );
}

/** Millièmes d'euro → chaîne lisible, pour le tableau de bord de dépense. */
export function formatEuros(milliCents: number): string {
  return `${(milliCents / 1000).toFixed(milliCents < 100 ? 4 : 2)} €`;
}
