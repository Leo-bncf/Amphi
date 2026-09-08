/**
 * Mesure du taux d'erreur mot (WER) — §5.4.
 *
 * Sert deux usages : le banc synthétique qui doit démontrer que la fusion fait
 * mieux que le meilleur flux isolé, et la mesure sur un vrai cours transcrit à
 * la main. Les deux ont besoin de la même normalisation, sinon les chiffres ne
 * sont pas comparables.
 */

export interface WerResult {
  readonly wer: number;
  readonly substitutions: number;
  readonly deletions: number;
  readonly insertions: number;
  readonly referenceWords: number;
}

/**
 * Normalisation avant comparaison : casse, accents, ponctuation, espaces.
 *
 * On retire les accents parce qu'une ASR qui écrit « hétéroscedasticité » au lieu
 * de « hétéroscédasticité » n'a pas commis d'erreur de reconnaissance utile à
 * compter — alors qu'un mot manquant, si.
 */
export function normalizeForWer(text: string): string[] {
  return text
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9\s]/g, " ")
    .split(/\s+/)
    .filter((token) => token !== "");
}

type Op = "match" | "sub" | "del" | "ins";

/** Levenshtein sur les tokens, avec comptage séparé des trois types d'erreur. */
export function wer(reference: string, hypothesis: string): WerResult {
  const ref = normalizeForWer(reference);
  const hyp = normalizeForWer(hypothesis);

  if (ref.length === 0) {
    return {
      wer: hyp.length === 0 ? 0 : 1,
      substitutions: 0,
      deletions: 0,
      insertions: hyp.length,
      referenceWords: 0,
    };
  }

  // Programmation dynamique classique : on garde la matrice pour pouvoir
  // remonter le chemin et distinguer substitutions, omissions et insertions.
  const rows = ref.length + 1;
  const cols = hyp.length + 1;
  const cost = new Int32Array(rows * cols);
  const from = new Uint8Array(rows * cols);

  const OP: Record<Op, number> = { match: 0, sub: 1, del: 2, ins: 3 };

  for (let i = 1; i < rows; i += 1) {
    cost[i * cols] = i;
    from[i * cols] = OP.del;
  }
  for (let j = 1; j < cols; j += 1) {
    cost[j] = j;
    from[j] = OP.ins;
  }

  for (let i = 1; i < rows; i += 1) {
    for (let j = 1; j < cols; j += 1) {
      const same = ref[i - 1] === hyp[j - 1];
      const subCost = (cost[(i - 1) * cols + (j - 1)] as number) + (same ? 0 : 1);
      const delCost = (cost[(i - 1) * cols + j] as number) + 1;
      const insCost = (cost[i * cols + (j - 1)] as number) + 1;

      let best = subCost;
      let op = same ? OP.match : OP.sub;
      if (delCost < best) {
        best = delCost;
        op = OP.del;
      }
      if (insCost < best) {
        best = insCost;
        op = OP.ins;
      }
      cost[i * cols + j] = best;
      from[i * cols + j] = op;
    }
  }

  let substitutions = 0;
  let deletions = 0;
  let insertions = 0;
  let i = ref.length;
  let j = hyp.length;
  while (i > 0 || j > 0) {
    const op = from[i * cols + j];
    if (op === OP.match) {
      i -= 1;
      j -= 1;
    } else if (op === OP.sub) {
      substitutions += 1;
      i -= 1;
      j -= 1;
    } else if (op === OP.del) {
      deletions += 1;
      i -= 1;
    } else {
      insertions += 1;
      j -= 1;
    }
  }

  return {
    wer: (substitutions + deletions + insertions) / ref.length,
    substitutions,
    deletions,
    insertions,
    referenceWords: ref.length,
  };
}
