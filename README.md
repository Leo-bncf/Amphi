# Amphi

Prise de notes de cours en amphi : plusieurs téléphones enregistrent la même séance, la transcription est fusionnée en une version canonique meilleure que chaque flux isolé, et un LLM en tire des notes structurées dont **chaque affirmation renvoie à un passage horodaté**.

Conçu pour une promo — 10 à 30 personnes, ~30 h de cours par semaine — pour environ **2,80 €/mois**.

📐 **[ARCHITECTURE.md](./ARCHITECTURE.md)** — décisions, coûts, risques, jalons. À lire avant de toucher au code.

## État

| Jalon | Contenu | État |
|---|---|---|
| M0 | Architecture, coûts, décisions | ✅ validé |
| **M1** | Enregistrement mono-appareil → ASR → notes → lecture horodatée | 🚧 en cours |
| M2 | Édition collaborative, Mermaid, Excalidraw | à venir |
| M3 | Multi-appareils, consensus, affichage des désaccords | à venir |
| M4 | Import ICS, recherche sémantique | à venir |
| M5 | Slides et photos, flashcards, exports | à venir |

M1 s'ouvre sur trois mesures, avant toute fonctionnalité, parce qu'elles peuvent invalider des pans entiers du plan :

1. **Capture 90 min sur iPhone, écran verrouillé** — Safari suspend la capture en arrière-plan, et le scénario nominal est « téléphone posé sur la table ».
2. ✅ **Débit de Whisper sur le Mac** — mesuré à **9,9× le temps réel** sur M4 (il en faut 3 pour tenir trois flux en direct). → `bench/results/whisper-m4.json`
3. **Test à l'aveugle du document final** — modèle local, Mistral Small 4, Haiku 4.5, Sonnet 5, sur un vrai cours.

## Prérequis

- **Node ≥ 24** et **pnpm ≥ 12** (`npm i -g pnpm`)
- **Python 3.11+** pour le banc de mesure (`/usr/bin/python3` convient)
- **Docker** pour Postgres et Redis en local — ⚠️ pas installé sur cette machine, à poser avant de lancer l'API (Docker Desktop ou OrbStack)

Aucun Homebrew requis : le worker ASR passe par MLX, qui s'installe avec pip et n'a rien à compiler.

## Démarrage

```bash
pnpm install
pnpm typecheck
pnpm test
```

### Banc de mesure Whisper

```bash
cd bench
python3 -m venv .venv && ./.venv/bin/pip install mlx-whisper
./.venv/bin/python bench_whisper.py
```

Le premier passage télécharge le modèle (~1,6 Go) — c'est normal qu'il soit long. Le
script mesure le régime à chaud, celui du worker, et écrit `bench/results/whisper-m4.json`.

Pour régénérer l'audio de test à partir du texte de référence :

```bash
say -v Jacques -f fixtures/cours-regularisation.txt -o /tmp/cours.aiff
afconvert -f WAVE -d LEI16@16000 -c 1 /tmp/cours.aiff fixtures/cours-regularisation.wav
```

C'est de la synthèse vocale, donc anormalement propre : le WER obtenu est un plancher de validation, pas une prévision de terrain. La vraie mesure se fera sur un cours enregistré.

## Structure

```
apps/web          PWA Next.js — capture, lecture, édition
apps/api          API Fastify — REST + WebSocket de session
apps/realtime     Hocuspocus (Yjs) + présence
apps/worker       Jobs BullMQ — ASR, consensus, génération de notes
apps/mac-worker   Worker ASR local opportuniste (MLX), ADR-15
packages/shared   Types, schémas zod, interfaces de fournisseurs
packages/db       Schéma Drizzle et migrations
packages/consensus Fusion multi-flux — TypeScript pur, sans I/O
bench             Bancs de mesure : Whisper, WER, comparaison de modèles
```

## Conventions

- TypeScript strict, **aucun `any`**, validation zod à toutes les frontières.
- Les fournisseurs externes (ASR, LLM, stockage) sont derrière des interfaces : en changer est une ligne de configuration, jamais une réécriture.
- `packages/consensus` reste pur et sans I/O — c'est ce qui le rend testable sur des cas synthétiques comme sur des séances réelles rejouées.
- Un bloc de notes généré sans ancre valide vers la transcription est **écarté**, pas affiché.
