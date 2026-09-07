/**
 * Modèle d'horloge de session — ADR-03.
 *
 * Trois horloges coexistent et il ne faut pas les confondre :
 *   - `Date.now()` côté client : sujette aux sauts NTP, inutilisable pour aligner
 *     de l'audio à la centaine de millisecondes près ;
 *   - le compteur d'échantillons de l'AudioWorklet : c'est la référence LOCALE,
 *     monotone, mais dont le quartz dérive de 10 à 100 ppm par rapport aux voisins
 *     (jusqu'à 360 ms sur une heure — le terme dominant, devant l'offset initial) ;
 *   - le temps de session côté serveur : la référence COMMUNE.
 *
 * D'où un modèle affine par participant, et non un simple décalage :
 *
 *     t_session = a · t_local + b
 *
 * `b` est estimé au join (voir `estimateOffsetMs`), `a` par ré-ancrage continu
 * pendant la séance (packages/consensus). Un calage unique sur les 60 premières
 * secondes ne peut PAS tenir le critère « < 200 ms après 60 min » : il corrige b
 * et laisse courir a.
 */

export interface ClockModel {
  /** Pente : rapport des fréquences d'horloge. 1 = pas de dérive. */
  readonly a: number;
  /** Décalage en millisecondes à t_local = 0. */
  readonly b: number;
}

/** Horloge du participant de référence, ou absence d'estimation. */
export const IDENTITY_CLOCK: ClockModel = { a: 1, b: 0 };

export function toSessionMs(clock: ClockModel, localMs: number): number {
  return clock.a * localMs + clock.b;
}

export function fromSessionMs(clock: ClockModel, sessionMs: number): number {
  return (sessionMs - clock.b) / clock.a;
}

/** Dérive résiduelle, en millisecondes, accumulée sur une durée donnée. */
export function driftOverMs(clock: ClockModel, durationMs: number): number {
  return (clock.a - 1) * durationMs;
}

/** Dérive exprimée en parties par million, l'unité usuelle pour les quartz. */
export function driftPpm(clock: ClockModel): number {
  return (clock.a - 1) * 1_000_000;
}

/**
 * Un aller-retour d'horloge, façon NTP simplifié (algorithme de Cristian).
 * Les quatre horodatages sont pris dans cet ordre chronologique.
 */
export interface RoundTripSample {
  readonly clientSendMs: number;
  readonly serverRecvMs: number;
  readonly serverSendMs: number;
  readonly clientRecvMs: number;
}

export interface OffsetEstimate {
  /** À ajouter au temps client pour obtenir le temps serveur. */
  readonly offsetMs: number;
  /** RTT du meilleur échantillon retenu — indicateur de confiance. */
  readonly bestRttMs: number;
  /** Nombre d'échantillons effectivement retenus dans la médiane. */
  readonly usedSamples: number;
}

function offsetOf(s: RoundTripSample): number {
  return (s.serverRecvMs - s.clientSendMs + (s.serverSendMs - s.clientRecvMs)) / 2;
}

function rttOf(s: RoundTripSample): number {
  return s.clientRecvMs - s.clientSendMs - (s.serverSendMs - s.serverRecvMs);
}

function median(values: readonly number[]): number {
  const sorted = [...values].sort((x, y) => x - y);
  const mid = sorted.length >> 1;
  if (sorted.length % 2 === 1) return sorted[mid] as number;
  return ((sorted[mid - 1] as number) + (sorted[mid] as number)) / 2;
}

/**
 * Estime le décalage d'horloge à partir de N allers-retours.
 *
 * On ne prend PAS la médiane de tous les échantillons : un RTT élevé est presque
 * toujours asymétrique (une seule direction a été retardée), ce qui biaise
 * l'estimation. On garde donc le quartile de plus faible RTT — au minimum trois
 * échantillons quand il y en a assez — et on prend la médiane de ceux-là.
 */
export function estimateOffsetMs(samples: readonly RoundTripSample[]): OffsetEstimate {
  if (samples.length === 0) {
    throw new Error("estimateOffsetMs: au moins un échantillon est requis");
  }

  const ranked = [...samples].sort((x, y) => rttOf(x) - rttOf(y));
  const keep = Math.max(1, Math.min(ranked.length, Math.max(3, Math.ceil(ranked.length / 4))));
  const best = ranked.slice(0, keep);

  return {
    offsetMs: median(best.map(offsetOf)),
    bestRttMs: rttOf(ranked[0] as RoundTripSample),
    usedSamples: best.length,
  };
}

/** Nombre d'allers-retours effectués au join. Compromis latence / précision. */
export const CLOCK_SYNC_ROUND_TRIPS = 7;

/**
 * Au-delà de ce résidu, un flux est marqué `desynced` et exclu du vote de
 * consensus jusqu'à re-convergence. Vient du critère d'acceptation n°3.
 */
export const MAX_CLOCK_RESIDUAL_MS = 200;
