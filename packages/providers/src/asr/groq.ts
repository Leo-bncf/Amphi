import { type AsrProvider, type AsrRequest, type AsrResult, type AsrSegment } from "@amphi/shared";
import { z } from "zod";
import { asrCostMilliCents } from "../pricing.js";

/**
 * Repli payant — ADR-01. 0,037 €/h, facturé au minimum 10 s par requête.
 *
 * Groq ne renvoie pas de probabilité par mot, seulement un `avg_logprob` par
 * segment. On en dérive une confiance : exp(avg_logprob), ce qui est l'estimation
 * usuelle. Elle est moins fine que celle du moteur local, ce qui affaiblit un peu
 * le vote de consensus — une raison de plus pour que le local soit le défaut.
 */

const GroqWord = z.object({
  word: z.string(),
  start: z.number(),
  end: z.number(),
});

const GroqSegment = z.object({
  start: z.number(),
  end: z.number(),
  text: z.string(),
  avg_logprob: z.number().optional(),
  no_speech_prob: z.number().optional(),
});

const GroqResponse = z.object({
  text: z.string(),
  language: z.string().optional(),
  duration: z.number().optional(),
  segments: z.array(GroqSegment).optional(),
  words: z.array(GroqWord).optional(),
});

export interface GroqConfig {
  readonly apiKey: string;
  readonly baseUrl?: string;
  readonly model?: string;
  readonly timeoutMs?: number;
}

const DEFAULT_BASE_URL = "https://api.groq.com/openai/v1";
const DEFAULT_MODEL = "whisper-large-v3-turbo";

function confidenceFrom(segment: z.infer<typeof GroqSegment>): number {
  if (segment.avg_logprob === undefined) return 0.5;
  const fromLogprob = Math.exp(segment.avg_logprob);
  const speech = 1 - (segment.no_speech_prob ?? 0);
  return Math.min(0.99, Math.max(0.01, fromLogprob * speech));
}

export class GroqAsrProvider implements AsrProvider {
  readonly name = "groq";
  readonly isLocal = false;

  constructor(private readonly config: GroqConfig) {
    if (config.apiKey === "") throw new Error("GroqAsrProvider: clé API manquante");
  }

  async isAvailable(): Promise<boolean> {
    return this.config.apiKey !== "";
  }

  async transcribe(request: AsrRequest): Promise<AsrResult> {
    const model = this.config.model ?? DEFAULT_MODEL;
    const form = new FormData();
    form.append("file", new Blob([new Uint8Array(request.audio)], { type: request.mimeType }), "chunk");
    form.append("model", model);
    form.append("response_format", "verbose_json");
    form.append("timestamp_granularities[]", "word");
    form.append("timestamp_granularities[]", "segment");
    // Pas de `language` : les cours mélangent français et anglais et forcer une
    // langue fait halluciner sur les passages de l'autre.
    form.append("temperature", "0");

    const prompt = [request.previousText, request.lexicon?.slice(0, 60).join(", ")]
      .filter((part): part is string => part !== undefined && part !== "")
      .join(" ")
      .slice(-800);
    if (prompt !== "") form.append("prompt", prompt);

    const startedAt = Date.now();
    const response = await fetch(`${this.config.baseUrl ?? DEFAULT_BASE_URL}/audio/transcriptions`, {
      method: "POST",
      headers: { Authorization: `Bearer ${this.config.apiKey}` },
      body: form,
      signal: AbortSignal.timeout(this.config.timeoutMs ?? 120_000),
    });

    if (!response.ok) {
      const detail = await response.text().catch(() => "");
      throw new Error(`groq: HTTP ${response.status} ${detail.slice(0, 300)}`);
    }

    const parsed = GroqResponse.parse(await response.json());
    const durationS = parsed.duration ?? 0;
    const words = parsed.words ?? [];

    const segments: AsrSegment[] = (parsed.segments ?? []).map((segment) => {
      const confidence = confidenceFrom(segment);
      // Les mots arrivent à plat : on les redistribue par recouvrement temporel.
      const owned = words.filter((w) => w.start >= segment.start - 0.01 && w.end <= segment.end + 0.01);
      return {
        startMs: segment.start * 1000,
        endMs: segment.end * 1000,
        text: segment.text.trim(),
        words: owned.map((w) => ({
          text: w.word.trim(),
          startMs: w.start * 1000,
          endMs: w.end * 1000,
          confidence,
        })),
        lang: parsed.language ?? null,
        avgConfidence: confidence,
      };
    });

    return {
      segments,
      audioDurationMs: durationS * 1000,
      provider: this.name,
      model,
      costMilliCents: asrCostMilliCents("groq", durationS),
      processingMs: Date.now() - startedAt,
    };
  }
}
