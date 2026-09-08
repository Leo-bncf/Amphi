import type { CostProfile, ParticipantId } from "@amphi/shared";
import type { AsrProviderKey } from "../pricing.js";

/**
 * Choix du moteur et du nombre de flux à transcrire — ADR-15 et §5.0.
 *
 * Fonction pure : elle décide, elle n'exécute pas. C'est ce qui permet de la
 * tester exhaustivement, y compris les cas dégradés qui n'arrivent qu'une fois
 * par mois en vrai et qu'on ne verrait jamais autrement.
 *
 * La règle qui compte : quand le moteur local n'est pas disponible, on dégrade
 * **le nombre de flux en même temps que le fournisseur**, pas seulement le
 * fournisseur. C'est ce qui borne la dépense du pire cas — un mois entier sans
 * le Mac — à 7,14 € au lieu de 21 €.
 */

export interface StreamCandidate {
  readonly participantId: ParticipantId;
  /** Score composite SNR / énergie vocale / taux de trous. `null` = pas encore mesuré. */
  readonly qualityScore: number | null;
  /** Un flux troué est inutilisable pour le vote : il décale tout l'alignement. */
  readonly hasRecentGap: boolean;
}

export interface RoutingInput {
  readonly candidates: readonly StreamCandidate[];
  readonly profile: CostProfile;
  /** Le worker Mac répond-il, et a-t-il assez de batterie ? */
  readonly localAvailable: boolean;
  /** Plafond mensuel atteint : on préfère différer que dépenser. */
  readonly budgetExhausted: boolean;
}

export type RoutingPlan =
  | {
      readonly kind: "transcribe";
      readonly provider: AsrProviderKey;
      readonly participantIds: readonly ParticipantId[];
      /** Vrai quand on transcrit moins de flux que le profil ne le demande. */
      readonly degraded: boolean;
      readonly reason: string;
    }
  | {
      readonly kind: "defer";
      readonly reason: string;
    };

/** Au-delà, le gain de la fusion ne paie plus le temps GPU, même gratuit. */
export const MAX_LOCAL_STREAMS = 5;

/** Nombre de flux visé par profil quand tout va bien (§11.4). */
const TARGET_STREAMS: Record<CostProfile, number> = {
  minimal: 1,
  suivi: 1,
  fusion: 3,
  max: 3,
};

const DEFAULT_QUALITY = 0.5;

function rank(candidates: readonly StreamCandidate[]): StreamCandidate[] {
  // Un flux troué passe toujours après un flux intact, quelle que soit sa qualité.
  return [...candidates].sort((a, b) => {
    if (a.hasRecentGap !== b.hasRecentGap) return a.hasRecentGap ? 1 : -1;
    return (b.qualityScore ?? DEFAULT_QUALITY) - (a.qualityScore ?? DEFAULT_QUALITY);
  });
}

export function planTranscription(input: RoutingInput): RoutingPlan {
  const ranked = rank(input.candidates);
  if (ranked.length === 0) {
    return { kind: "defer", reason: "aucun flux candidat" };
  }

  const target = TARGET_STREAMS[input.profile];

  if (input.localAvailable) {
    // Le moteur local est gratuit : on transcrit tout ce qui est exploitable, ce
    // qui rend la fusion possible par défaut au lieu d'être un luxe.
    const take = Math.min(ranked.length, MAX_LOCAL_STREAMS);
    return {
      kind: "transcribe",
      provider: "mlx-local",
      participantIds: ranked.slice(0, take).map((c) => c.participantId),
      degraded: false,
      reason: `moteur local disponible, ${take} flux transcrits`,
    };
  }

  if (input.budgetExhausted) {
    // On ne perd rien : l'audio dort 7 jours et la séance pourra être rebasculée
    // en fusion rétroactivement dès que le Mac se rebranche.
    return {
      kind: "defer",
      reason: "plafond mensuel atteint, transcription différée jusqu'au retour du moteur local",
    };
  }

  const take = Math.min(ranked.length, 1);
  return {
    kind: "transcribe",
    provider: "groq",
    participantIds: ranked.slice(0, take).map((c) => c.participantId),
    degraded: target > take,
    reason:
      target > take
        ? `repli payant : ${take} flux au lieu de ${target}, fusion reportée`
        : "repli payant, profil déjà à un seul flux",
  };
}
