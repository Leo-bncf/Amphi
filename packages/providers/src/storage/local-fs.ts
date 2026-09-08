import { createHash } from "node:crypto";
import { mkdir, readFile, rm, stat, writeFile } from "node:fs/promises";
import { dirname, join, normalize, resolve, sep } from "node:path";

/**
 * Stockage des chunks audio — ADR-05.
 *
 * Système de fichiers local plutôt qu'un object storage S3 : 11 Mo par heure et
 * par flux, soit 26 Go pour une année complète à trois flux. Ce n'est pas un poste
 * de coût, c'est une case à cocher. L'interface reste en place pour que passer à
 * Scaleway Object Storage un jour ne soit pas une réécriture.
 */

export interface StorageProvider {
  readonly name: string;
  put(key: string, data: Uint8Array): Promise<void>;
  get(key: string): Promise<Uint8Array>;
  delete(key: string): Promise<void>;
  exists(key: string): Promise<boolean>;
}

/**
 * Clé de stockage déterministe. Le hash du participant évite d'exposer des UUID
 * dans l'arborescence, et le découpage en sous-dossiers évite un répertoire à
 * cent mille entrées après quelques mois.
 */
export function chunkStorageKey(sessionId: string, participantId: string, seq: number): string {
  const shard = createHash("sha256").update(participantId).digest("hex").slice(0, 2);
  return `sessions/${sessionId}/${shard}/${participantId}/${String(seq).padStart(6, "0")}.ogg`;
}

export class LocalFsStorage implements StorageProvider {
  readonly name = "local-fs";

  private readonly root: string;

  constructor(root: string) {
    this.root = resolve(root);
  }

  /**
   * Les clés viennent de la base, mais elles ont pu être écrites par une version
   * antérieure du code ou une migration : on vérifie qu'on ne sort pas de la
   * racine plutôt que de faire confiance.
   */
  private pathFor(key: string): string {
    const full = resolve(join(this.root, normalize(key)));
    if (full !== this.root && !full.startsWith(this.root + sep)) {
      throw new Error(`clé de stockage hors racine: ${key}`);
    }
    return full;
  }

  async put(key: string, data: Uint8Array): Promise<void> {
    const path = this.pathFor(key);
    await mkdir(dirname(path), { recursive: true });
    // Écriture atomique : un chunk à moitié écrit qu'un worker lirait pendant un
    // crash produirait une transcription tronquée, silencieusement.
    const temp = `${path}.${process.pid}.tmp`;
    await writeFile(temp, data);
    const { rename } = await import("node:fs/promises");
    await rename(temp, path);
  }

  async get(key: string): Promise<Uint8Array> {
    return new Uint8Array(await readFile(this.pathFor(key)));
  }

  async delete(key: string): Promise<void> {
    // `force` : supprimer deux fois n'est pas une erreur, la purge RGPD est rejouable.
    await rm(this.pathFor(key), { force: true });
  }

  async exists(key: string): Promise<boolean> {
    try {
      await stat(this.pathFor(key));
      return true;
    } catch {
      return false;
    }
  }
}
