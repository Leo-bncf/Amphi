import type { z } from "zod";

/**
 * Interface LLM — ADR-16.
 *
 * Implémentations prévues, interchangeables par configuration :
 *   - Mistral Small 4 (défaut, document final, 0,0052 € par cours)
 *   - modèle local via MLX sur le Mac (résumés incrémentaux, gratuit)
 *   - Claude Sonnet 5 / Opus 5 (régénération manuelle « qualité »)
 *
 * Sous Haiku 4.5, les fournisseurs se tiennent à 22 centimes par mois : le prix
 * a cessé d'être un critère de choix, seule la qualité des notes compte. Le test
 * à l'aveugle de M1 tranche.
 */

export interface LlmUsage {
  readonly inputTokens: number;
  readonly outputTokens: number;
  /** Coût réel en millièmes d'euro. 0 pour un modèle local. */
  readonly costMilliCents: number;
}

export interface LlmResult<T> {
  readonly value: T;
  readonly usage: LlmUsage;
  readonly provider: string;
  readonly model: string;
  readonly latencyMs: number;
}

export interface LlmRequest<T> {
  readonly system: string;
  readonly user: string;
  /** La sortie est contrainte par ce schéma, puis validée. Pas de parsing au jugé. */
  readonly schema: z.ZodType<T>;
  readonly maxOutputTokens: number;
  /** Partie stable du prompt, à mettre en cache quand le fournisseur le permet. */
  readonly cacheablePrefix?: string;
}

export interface LlmProvider {
  readonly name: string;
  readonly isLocal: boolean;
  complete<T>(request: LlmRequest<T>): Promise<LlmResult<T>>;
  isAvailable(): Promise<boolean>;
}
