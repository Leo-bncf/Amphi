import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { LocalFsStorage, chunkStorageKey } from "./local-fs.js";

describe("LocalFsStorage", () => {
  let root: string;
  let storage: LocalFsStorage;

  beforeEach(async () => {
    root = await mkdtemp(join(tmpdir(), "amphi-storage-"));
    storage = new LocalFsStorage(root);
  });

  afterEach(async () => {
    await rm(root, { recursive: true, force: true });
  });

  it("écrit puis relit un chunk à l'identique", async () => {
    const key = chunkStorageKey("s1", "p1", 42);
    const data = new Uint8Array([0x4f, 0x67, 0x67, 0x53, 0x00, 0xff]);
    await storage.put(key, data);
    expect(await storage.get(key)).toEqual(data);
    expect(await storage.exists(key)).toBe(true);
  });

  it("écrase proprement lors d'un rejeu de la file offline", async () => {
    // Un chunk retenté doit produire le même état final, pas une concaténation.
    const key = chunkStorageKey("s1", "p1", 7);
    await storage.put(key, new Uint8Array([1, 2, 3]));
    await storage.put(key, new Uint8Array([9]));
    expect(await storage.get(key)).toEqual(new Uint8Array([9]));
  });

  it("supprime sans erreur une clé absente, pour que la purge RGPD soit rejouable", async () => {
    await expect(storage.delete("sessions/absent/00/x/000001.ogg")).resolves.toBeUndefined();
  });

  it("refuse une clé qui sort de la racine", async () => {
    await expect(storage.put("../evasion.ogg", new Uint8Array([1]))).rejects.toThrow(/hors racine/);
    await expect(storage.get("sessions/../../etc/passwd")).rejects.toThrow(/hors racine/);
  });

  it("répartit les participants en sous-dossiers et garde les séquences ordonnées", () => {
    const a = chunkStorageKey("s1", "participant-a", 1);
    const b = chunkStorageKey("s1", "participant-b", 1);
    expect(a).not.toBe(b);
    expect(chunkStorageKey("s1", "p", 2) > chunkStorageKey("s1", "p", 1)).toBe(true);
    // Le tri lexicographique doit suivre l'ordre des séquences au-delà de 10.
    expect(chunkStorageKey("s1", "p", 10) > chunkStorageKey("s1", "p", 9)).toBe(true);
  });
});
