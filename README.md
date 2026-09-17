# Amphi

Amphi est une application open source de prise de notes pour les cours en amphi.

Plusieurs appareils peuvent enregistrer la même séance. Les transcriptions sont ensuite fusionnées en une version canonique plus fiable que chaque flux isolé, puis un LLM génère des notes structurées dont **chaque affirmation renvoie à un passage horodaté du cours**.

Conçu pour une promo de **10 à 30 personnes**, avec environ **30 h de cours par semaine**, pour un coût cible d'environ **2,80 €/mois**.

> 📐 **[ARCHITECTURE.md](./ARCHITECTURE.md)** — architecture, décisions techniques, coûts, risques et jalons. À lire avant de toucher au code.

---

## Télécharger Amphi

Si tu veux simplement utiliser Amphi, **inutile de cloner le dépôt ou de compiler le projet**.

Les versions installables sont publiées directement dans les **Releases GitHub** :

### **[Télécharger la dernière version](../../releases/latest)**

Choisis le build correspondant à ton système, puis suis les instructions indiquées dans la release.

> Le développement se concentre actuellement principalement sur **macOS**.

---

## État du projet

| Jalon | Contenu | État |
|---|---|---|
| M0 | Architecture, coûts, décisions | ✅ Validé |
| **M1** | Enregistrement → ASR → notes ancrées → lecture horodatée | ✅ Démontrable |
| M1+ | Import photos/documents, notes éditables, diagrammes Mermaid | ✅ Démontrable |
| **M2** | Application desktop Tauri + transcription native embarquée | 🚧 En cours |
| M3 | Multi-appareils, consensus, affichage des désaccords | ⏳ À venir |
| M4 | Import ICS, recherche sémantique | ⏳ À venir |
| M5 | Slides, photos, flashcards, exports | ⏳ À venir |

M1 s'ouvre volontairement sur trois mesures capables d'invalider des pans entiers du plan :

1. **Capture 90 min sur iPhone, écran verrouillé** — Safari suspend la capture en arrière-plan alors que le scénario nominal est « téléphone posé sur la table ».
2. ✅ **Débit de Whisper sur Mac** — mesuré à **9,9× le temps réel** sur M4. Il en faut environ 3× pour tenir trois flux en direct.  
   → `bench/results/whisper-m4.json`
3. **Test à l'aveugle du document final** — comparaison entre modèle local, Mistral Small 4, Haiku 4.5 et Sonnet 5 sur un vrai cours.

---

## Fonctionnement

```text
plusieurs appareils
        ↓
enregistrement audio
        ↓
ASR / Whisper
        ↓
plusieurs transcriptions
        ↓
alignement + consensus
        ↓
transcription canonique
        ↓
génération LLM
        ↓
validation des ancres
        ↓
notes structurées
        ↓
passages horodatés vérifiables
```

Le but d'Amphi n'est pas seulement de transformer de l'audio en résumé.

La transcription reste la **source de vérité** du système.

Chaque bloc généré doit pouvoir être relié à un passage précis du cours. Si la source citée ne justifie pas le contenu généré, le bloc est **écarté**.

---

## Principes

Trois règles de fond sont visibles directement dans l'application :

- **rien d'inventé** — un bloc dont la source citée ne correspond pas au contenu est écarté, pas affiché ;
- **pas de section creuse** — si l'enseignant annonce un titre sans rien développer, aucune rubrique artificielle n'est créée ;
- **les compléments sont signalés** — les informations ajoutées en dehors du cours sont explicitement marquées et ne sont jamais mélangées silencieusement au contenu original.

---

## Fonctionnalités

### Capture et transcription

- 🎙️ enregistrement micro ;
- 🧠 transcription locale avec Whisper ;
- ⏱️ timestamps cliquables ;
- ▶️ lecture depuis un passage précis ;
- ⚡ worker natif MLX ;
- 💻 traitement local quand possible ;
- 👥 plusieurs appareils pour une même séance ;
- 🔀 fusion de plusieurs transcriptions.

### Notes

- 📝 génération de notes structurées ;
- 🔗 validation des sources ;
- ✏️ notes éditables ;
- ∑ formules LaTeX avec KaTeX ;
- 📊 diagrammes Mermaid ;
- 🚫 rejet automatique des blocs non supportés par une source ;
- 🧩 compléments externes clairement signalés.

### Documents

- 🖼️ import de photos du tableau ;
- 📄 import de documents ;
- 📚 organisation par matière ;
- 📂 organisation par chapitre ;
- 🔎 recherche plein texte.

### Export

- Markdown ;
- impression PDF ;
- autres exports prévus dans les prochains jalons.

---

## Prérequis

Pour travailler sur le projet :

- **Node.js ≥ 24**
- **pnpm ≥ 12**
- **Python 3.11+**
- **Docker**
- **Rust / Cargo**
- **cmake** pour certains builds desktop

Installer pnpm :

```bash
npm install -g pnpm
```

Docker est utilisé pour **Postgres** et **Redis** dans l'environnement complet.

> L'application locale `studio` peut fonctionner sans Docker ni base de données.

Aucun Homebrew n'est requis pour le worker ASR : MLX s'installe directement avec `pip`.

---

## Installation développement

```bash
pnpm install
pnpm typecheck
pnpm test
```

---

## Lancer l'application locale

```bash
./bench/.venv/bin/python apps/studio/server.py
```

Puis ouvrir :

```text
http://127.0.0.1:8765
```

La version locale comprend notamment :

- bibliothèque classée par matière et chapitre ;
- recherche plein texte ;
- enregistrement micro ;
- transcription locale ;
- horodatages cliquables ;
- import de photos ;
- import de documents ;
- notes éditables ;
- formules KaTeX ;
- diagrammes Mermaid ;
- export Markdown ;
- impression PDF.

**Aucun Docker et aucune base de données ne sont nécessaires pour cette version.**

Tout tourne directement sur la machine.

---

## Interface

Le système visuel suit **[Hallmark](https://www.usehallmark.com)** :

```bash
npx skills add nutlope/hallmark
```

Principes utilisés :

- palette OKLCH avec une seule teinte d'ancrage ;
- paire typographique **Bricolage Grotesque / Cardo** ;
- polices vendorisées localement ;
- échelle d'espacement de **4 pt** ;
- pas de bande latérale permanente ;
- pas de dégradés.

Les polices sont servies depuis :

```text
apps/studio/ui/vendor/fonts/
```

Aucun CDN n'est nécessaire : l'application reste lisible sans connexion réseau.

---

## Configuration LLM

La clé Mistral se place dans un fichier `.env` à la racine :

```env
MISTRAL_API_KEY=your_key_here
```

Le fichier `.env` n'est pas suivi par Git.

Sans clé Mistral :

- la transcription continue de fonctionner ;
- la génération de notes peut basculer vers un modèle MLX local lorsqu'il est disponible.

---

## Vérification des ancres

```bash
./bench/.venv/bin/python apps/studio/test_anchors.py
```

La suite couvre actuellement **11 cas** autour de la validation des liens entre notes générées et transcription.

Un bloc de notes sans ancre valide vers la transcription est **écarté**, pas affiché comme information fiable.

---

## Application desktop

Compiler l'application Tauri :

```bash
cd apps/desktop/src-tauri
cargo build --release
```

Le paquet macOS est généré dans :

```text
apps/desktop/dist-app/Amphi.app
```

L'adresse du serveur se règle dans :

```text
~/Library/Application Support/Amphi/server.txt
```

Les secrets ne sont jamais stockés dans ce fichier.

Sur macOS, ajouter au trousseau Keychain les entrées :

```text
server-token
server-password
```

avec le service :

```text
Amphi
```

Pour les overrides explicites de développement :

```bash
AMPHI_TOKEN=
AMPHI_PASSWORD=
```

Sous Linux, l'application n'utilise aucun fichier de secrets et reste sans authentification tant qu'aucune variable d'environnement n'est fournie.

---

## Pourquoi une application native

Mesuré sur la même machine et avec le même fichier audio :

```text
whisper-base / WebGPU      ≈ 3,9× temps réel
large-v3-turbo / WebGPU    ≈ 0,5× estimé
large-v3-turbo / MLX       ≈ 9,9× temps réel
```

Dans le navigateur, `whisper-base` atteint environ **3,9× le temps réel**, mais la qualité obtenue est insuffisante pour le scénario cible.

`large-v3-turbo`, qui préserve davantage la qualité, tomberait autour de **0,5×** dans ce contexte.

Le moteur natif MLX atteint environ **9,9×** avec ce même grand modèle.

Ce facteur change directement l'architecture et le coût du projet : l'étudiant peut transcrire localement sur sa machine plutôt que d'envoyer systématiquement les enregistrements vers un serveur payant.

---

## Benchmark Whisper

Créer l'environnement :

```bash
cd bench

python3 -m venv .venv
./.venv/bin/pip install mlx-whisper
./.venv/bin/python bench_whisper.py
```

Le premier passage télécharge le modèle, environ **1,6 Go**.

C'est normal qu'il soit nettement plus long.

Le script mesure ensuite le régime à chaud utilisé par le worker et écrit les résultats dans :

```text
bench/results/whisper-m4.json
```

---

## Génération de l'audio de test

Pour régénérer l'audio à partir du texte de référence :

```bash
say -v Jacques \
  -f fixtures/cours-regularisation.txt \
  -o /tmp/cours.aiff

afconvert \
  -f WAVE \
  -d LEI16@16000 \
  -c 1 \
  /tmp/cours.aiff \
  fixtures/cours-regularisation.wav
```

L'audio est généré par synthèse vocale et est donc anormalement propre.

Le WER mesuré doit être considéré comme un **plancher de validation**, pas comme une estimation réaliste d'un cours enregistré dans un vrai amphi.

La mesure terrain devra être effectuée sur de vrais cours.

---

## Architecture

La documentation complète se trouve dans :

**[ARCHITECTURE.md](./ARCHITECTURE.md)**

Elle contient notamment :

- les décisions d'architecture ;
- les coûts ;
- les risques ;
- les fournisseurs ;
- les workers ;
- les ADR ;
- la stratégie de consensus ;
- les compromis navigateur / natif ;
- la stratégie de traitement local ;
- les différents jalons du projet.

---

## Structure du dépôt

```text
apps/web          PWA Next.js — capture, lecture, édition
apps/api          API Fastify — REST + WebSocket de session
apps/realtime     Hocuspocus (Yjs) + présence
apps/worker       Jobs BullMQ — ASR, consensus, génération de notes
apps/mac-worker   Worker ASR local opportuniste (MLX), ADR-15
apps/studio       Application locale légère
apps/desktop      Application de bureau Tauri

packages/shared   Types, schémas Zod, interfaces fournisseurs
packages/db       Schéma Drizzle et migrations
packages/consensus Fusion multi-flux — TypeScript pur, sans I/O

bench             Whisper, WER, benchmarks et comparaison de modèles
```

---

## Conventions

- TypeScript strict ;
- **aucun `any`** ;
- validation Zod à toutes les frontières ;
- fournisseurs ASR, LLM et stockage derrière des interfaces ;
- changer de fournisseur doit être un changement de configuration, pas une réécriture ;
- `packages/consensus` reste pur et sans I/O ;
- le consensus doit pouvoir être testé sur des cas synthétiques et sur des séances réelles rejouées ;
- un bloc de notes généré sans ancre valide vers la transcription est **écarté**.

---

## Roadmap

### M0 — Architecture

- [x] architecture initiale
- [x] modèle de coûts
- [x] choix techniques
- [x] risques principaux
- [x] décisions ADR

### M1 — Pipeline principal

- [x] capture audio
- [x] ASR
- [x] transcription
- [x] timestamps
- [x] génération de notes
- [x] validation des ancres
- [x] lecture horodatée

### M1+ — Enrichissement

- [x] import photos
- [x] import documents
- [x] notes éditables
- [x] LaTeX
- [x] Mermaid
- [x] export Markdown
- [x] impression PDF

### M2 — Desktop

- [x] base Tauri
- [x] worker MLX
- [ ] distribution stable
- [ ] packaging multi-plateforme
- [ ] transcription native complètement intégrée

### M3 — Multi-appareils

- [ ] plusieurs téléphones sur une même séance
- [ ] alignement des flux
- [ ] consensus
- [ ] détection des désaccords
- [ ] affichage des passages incertains

### M4 — Recherche

- [ ] import ICS
- [ ] recherche sémantique
- [ ] navigation avancée entre cours

### M5 — Révision

- [ ] slides
- [ ] photos enrichies
- [ ] flashcards
- [ ] exports supplémentaires

---

## Coût cible

Scénario prévu :

```text
10 à 30 étudiants
~30 h de cours / semaine
≈ 2,80 € / mois
```

Le coût réel dépend notamment :

- du volume de cours ;
- du nombre d'utilisateurs ;
- du modèle LLM choisi ;
- de la quantité de traitement local ;
- du stockage ;
- du nombre de flux simultanés.

L'objectif architectural est de garder autant de calcul que possible sur les machines des utilisateurs lorsque cela est pertinent.

---

## Sécurité

Principes actuels :

- secrets exclus du dépôt ;
- `.env` ignoré par Git ;
- credentials desktop stockés dans Keychain sur macOS ;
- overrides uniquement via variables d'environnement ;
- aucune clé stockée dans `server.txt` ;
- séparation entre configuration et secrets.

---

## Contribution

Les contributions sont bienvenues.

Avant de modifier une partie importante de l'architecture :

1. lire **[ARCHITECTURE.md](./ARCHITECTURE.md)** ;
2. respecter les conventions TypeScript et Zod ;
3. garder `packages/consensus` sans I/O ;
4. lancer :

```bash
pnpm typecheck
pnpm test
```

avant d'ouvrir une Pull Request.

---

## GitHub Topics

À ajouter dans **About → Topics** :

```text
education
students
university
lecture-notes
note-taking
transcription
speech-to-text
whisper
mlx
llm
local-ai
tauri
typescript
rust
nextjs
yjs
collaboration
edtech
open-source
student-tools
ai-notes
productivity
```

---

## Licence

Voir [`LICENSE`](./LICENSE).
