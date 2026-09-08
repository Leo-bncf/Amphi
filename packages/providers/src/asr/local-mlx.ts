import { AsrResult, type AsrProvider, type AsrRequest } from "@amphi/shared";

/**
 * Moteur de transcription local — ADR-15.
 *
 * Client du serveur Python (`apps/mac-worker/asr_server.py`), qui garde le modèle
 * MLX chargé en mémoire. On passe par HTTP sur la boucle locale plutôt que par un
 * protocole stdio : c'est inspectable avec curl quand ça ne marche pas, et le
 * serveur survit à un redémarrage du worker Node.
 */

export interface LocalMlxConfig {
  readonly baseUrl: string;
  /** Un chunk de 25 s doit revenir en quelques secondes ; au-delà, on bascule. */
  readonly timeoutMs: number;
}

export const DEFAULT_LOCAL_MLX_CONFIG: LocalMlxConfig = {
  baseUrl: "http://127.0.0.1:8765",
  timeoutMs: 60_000,
};

interface HealthResponse {
  readonly available: boolean;
  readonly warm: boolean;
  readonly onAcPower: boolean;
  readonly batteryPercent: number | null;
}

export class LocalMlxAsrProvider implements AsrProvider {
  readonly name = "mlx-local";
  readonly isLocal = true;

  private readonly config: LocalMlxConfig;

  constructor(config: Partial<LocalMlxConfig> = {}) {
    this.config = { ...DEFAULT_LOCAL_MLX_CONFIG, ...config };
  }

  /**
   * Indisponible signifie « le Mac dort, est éteint, ou est sur batterie faible ».
   * Ce n'est pas une erreur : c'est le cas nominal une partie de la journée, et
   * c'est précisément ce que le repli payant existe pour couvrir.
   */
  async isAvailable(): Promise<boolean> {
    try {
      const response = await fetch(`${this.config.baseUrl}/health`, {
        signal: AbortSignal.timeout(2_000),
      });
      if (!response.ok) return false;
      const health = (await response.json()) as HealthResponse;
      return health.available && health.warm;
    } catch {
      return false;
    }
  }

  async transcribe(request: AsrRequest): Promise<AsrResult> {
    const headers: Record<string, string> = {
      "Content-Type": "application/octet-stream",
      "X-Mime-Type": request.mimeType,
    };
    if (request.lexicon !== undefined && request.lexicon.length > 0) {
      headers["X-Lexicon"] = JSON.stringify(request.lexicon);
    }
    if (request.previousText !== undefined && request.previousText !== "") {
      // En-tête HTTP : pas de saut de ligne, et on borne la taille.
      headers["X-Previous-Text"] = request.previousText.replace(/\s+/g, " ").slice(-400);
    }

    const response = await fetch(`${this.config.baseUrl}/transcribe`, {
      method: "POST",
      headers,
      body: new Uint8Array(request.audio),
      signal: AbortSignal.timeout(this.config.timeoutMs),
    });

    if (!response.ok) {
      const detail = await response.text().catch(() => "");
      throw new Error(`moteur local: HTTP ${response.status} ${detail.slice(0, 200)}`);
    }

    // Le serveur est local et de confiance, mais on valide quand même : une
    // dérive de contrat doit échouer ici, pas trois étapes plus loin dans le
    // module de consensus avec des timestamps incohérents.
    return AsrResult.parse(await response.json());
  }
}
