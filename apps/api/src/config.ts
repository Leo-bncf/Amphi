import { z } from "zod";

/**
 * Configuration validée au démarrage. Un service qui démarre avec une variable
 * manquante puis échoue au premier enregistrement de cours est bien pire qu'un
 * service qui refuse de démarrer.
 */
const Config = z.object({
  NODE_ENV: z.enum(["development", "test", "production"]).default("development"),
  PORT: z.coerce.number().int().positive().default(3001),
  HOST: z.string().default("127.0.0.1"),

  DATABASE_URL: z.string().default("postgres://amphi:amphi@localhost:5433/amphi"),
  REDIS_URL: z.string().default("redis://localhost:6380"),

  /** Racine du stockage des chunks (ADR-05). */
  STORAGE_ROOT: z.string().default("./data/audio"),

  /** Repli payant. Absent = pas de repli, on diffère (voir planTranscription). */
  GROQ_API_KEY: z.string().optional(),
  MISTRAL_API_KEY: z.string().optional(),

  /**
   * Rétention de l'audio. 7 jours par défaut : c'est la fenêtre qui rend la
   * fusion rétroactive possible (§5.0). 0 = mode « transcription only » strict,
   * l'audio est supprimé dès la transcription réussie.
   */
  AUDIO_RETENTION_DAYS: z.coerce.number().int().min(0).default(7),

  /** Plafond mensuel en millièmes d'euro. 20 000 = 20 €. */
  MONTHLY_BUDGET_MILLI_CENTS: z.coerce.number().int().positive().default(20_000),

  /** En développement, le lien magique est écrit dans les logs au lieu d'être envoyé. */
  SMTP_URL: z.string().optional(),
});

export type Config = z.infer<typeof Config>;

export function loadConfig(env: NodeJS.ProcessEnv = process.env): Config {
  const parsed = Config.safeParse(env);
  if (!parsed.success) {
    const issues = parsed.error.issues.map((i) => `  ${i.path.join(".")}: ${i.message}`).join("\n");
    throw new Error(`Configuration invalide :\n${issues}`);
  }
  return parsed.data;
}
