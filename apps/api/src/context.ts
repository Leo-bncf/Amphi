import { createDatabase, type Database } from "@amphi/db";
import { LocalFsStorage, type StorageProvider } from "@amphi/providers";
import { Queue } from "bullmq";
import type { Config } from "./config.js";

/** Nom de la file de transcription, partagé avec apps/worker et apps/mac-worker. */
export const TRANSCRIBE_QUEUE = "amphi:transcribe";

export interface TranscribeJob {
  readonly chunkId: string;
  readonly sessionId: string;
  readonly participantId: string;
  readonly storageKey: string;
  readonly mimeType: string;
}

export interface AppContext {
  readonly config: Config;
  readonly db: Database;
  readonly storage: StorageProvider;
  readonly transcribeQueue: Queue<TranscribeJob>;
  close(): Promise<void>;
}

export function createContext(config: Config): AppContext {
  const db = createDatabase({ url: config.DATABASE_URL });
  const storage = new LocalFsStorage(config.STORAGE_ROOT);
  const transcribeQueue = new Queue<TranscribeJob>(TRANSCRIBE_QUEUE, {
    connection: { url: config.REDIS_URL },
    defaultJobOptions: {
      // Un chunk non transcrit est un trou dans les notes : on insiste longtemps.
      attempts: 8,
      backoff: { type: "exponential", delay: 5_000 },
      removeOnComplete: { age: 3_600, count: 1_000 },
      removeOnFail: false,
    },
  });

  return {
    config,
    db,
    storage,
    transcribeQueue,
    async close() {
      await transcribeQueue.close();
    },
  };
}
