import { describe, expect, it } from "vitest";
import { loadConfig } from "./config.js";

/**
 * Un service qui démarre avec une configuration douteuse puis échoue au premier
 * enregistrement de cours est bien pire qu'un service qui refuse de démarrer :
 * dans le premier cas, on découvre le problème avec une séance perdue.
 */

describe("loadConfig", () => {
  it("démarre sur des valeurs par défaut utilisables en local", () => {
    const config = loadConfig({});
    expect(config.PORT).toBe(3001);
    expect(config.HOST).toBe("127.0.0.1");
    expect(config.DATABASE_URL).toContain("5433");
    expect(config.AUDIO_RETENTION_DAYS).toBe(7);
  });

  it("accepte une rétention nulle — le mode « transcription only » strict", () => {
    // L'audio est supprimé dès la transcription réussie. C'est le mode le plus
    // conservateur du §10, au prix de la fusion rétroactive.
    expect(loadConfig({ AUDIO_RETENTION_DAYS: "0" }).AUDIO_RETENTION_DAYS).toBe(0);
  });

  it("refuse une rétention négative plutôt que de l'interpréter", () => {
    expect(() => loadConfig({ AUDIO_RETENTION_DAYS: "-1" })).toThrow(/AUDIO_RETENTION_DAYS/);
  });

  it("refuse un port non numérique en nommant la variable fautive", () => {
    expect(() => loadConfig({ PORT: "quatre-mille" })).toThrow(/PORT/);
  });

  it("laisse les clés de repli optionnelles — sans elles, on diffère au lieu de payer", () => {
    const config = loadConfig({});
    expect(config.GROQ_API_KEY).toBeUndefined();
    expect(config.MISTRAL_API_KEY).toBeUndefined();
  });

  it("lit le plafond mensuel depuis l'environnement", () => {
    expect(loadConfig({ MONTHLY_BUDGET_MILLI_CENTS: "5000" }).MONTHLY_BUDGET_MILLI_CENTS).toBe(5000);
  });
});
