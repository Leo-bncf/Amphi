# Amphi
Prise de notes de cours en amphi : plusieurs téléphones enregistrent la même séance, les transcriptions sont fusionnées en une version canonique plus fiable que chaque flux isolé, puis un LLM génère des notes structurées dont **chaque affirmation renvoie à un passage horodaté du cours**.
Conçu pour une promo de **10 à 30 personnes**, avec environ **30 h de cours par semaine**, pour un coût cible d'environ **2,80 €/mois**.
> 📐 **[ARCHITECTURE.md](./ARCHITECTURE.md)** — décisions techniques, coûts, risques et jalons. À lire avant de toucher à l'architecture.
---
## Installation
Si tu veux simplement utiliser Amphi, **inutile de cloner le dépôt ou de compiler l'application**.
Les builds sont disponibles directement dans :
### **[Releases → Latest release](../../releases/latest)**
Télécharge la version correspondant à ton système puis installe l'application normalement.
> Le développement se concentre actuellement principalement sur **macOS**. Le support des autres plateformes évoluera avec M2.
---
## État
| Jalon | Contenu | État |
|---|---|---|
| M0 | Architecture, coûts, décisions | ✅ validé |
| **M1** | Enregistrement → ASR → notes ancrées → lecture horodatée | ✅ démontrable |
| M1+ | Import photos/documents, notes éditables, diagrammes Mermaid | ✅ démontrable |
| **M2** | Application de bureau Tauri, transcription native embarquée | 🚧 en cours |
| M3 | Multi-appareils, consensus, affichage des désaccords | à venir |
| M4 | Import ICS, recherche sémantique | à venir |
| M5 | Slides et photos, flashcards, exports | à venir |
M1 commence volontairement par trois mesures susceptibles d'invalider des pans entiers de l'architecture :
1. **Capture 90 min sur iPhone, écran verrouillé** — Safari suspend la capture en arrière-plan, alors que le scénario nominal est « téléphone posé sur la table ».
2. ✅ **Débit Whisper sur Mac** — mesuré à **9,9× le temps réel** sur M4. Il en faut environ 3× pour tenir trois flux en direct.  
   → `bench/results/whisper-m4.json`
3. **Test à l'aveugle du document final** — comparaison entre modèle local, Mistral Small 4, Haiku 4.5 et Sonnet 5 sur un vrai cours.
---
## Pourquoi Amphi
Le problème n'est pas seulement de transformer de l'audio en résumé.
Le vrai problème est de pouvoir répondre à :
> **« D'où vient cette information ? »**
Amphi conserve donc la transcription comme source de vérité.
```text
plusieurs enregistrements
        ↓
transcriptions
        ↓
alignement + consensus
        ↓
transcription canonique
        ↓
génération des notes
        ↓
validation des ancres
        ↓
notes structurées et vérifiables

Chaque bloc généré doit pouvoir renvoyer vers le passage exact du cours qui le justifie.

S’il n’existe pas d’ancre valide, le bloc est écarté.

⸻

Principes

Trois règles de fond sont visibles directement dans l’application :

* rien d’inventé — un bloc dont la source citée ne correspond pas au contenu est écarté, pas affiché ;
* pas de section creuse — si l’enseignant annonce un titre sans rien développer, aucune rubrique n’est créée ;
* les compléments sont signalés — l’option « compléter les prérequis manquants » ajoute des blocs explicitement marqués hors cours, jamais mêlés au contenu de la séance.

⸻

Fonctionnalités actuelles

* 🎙️ enregistrement micro ;
* 🧠 transcription locale ;
* ⏱️ timestamps cliquables ;
* 📚 bibliothèque classée par matière et chapitre ;
* 🔎 recherche plein texte ;
* 🖼️ import de photos du tableau ;
* 📄 import de documents ;
* ✏️ notes éditables ;
* ∑ formules LaTeX via KaTeX ;
* 📊 diagrammes Mermaid ;
* 📤 export Markdown ;
* 🖨️ impression PDF ;
* 🔗 génération de notes ancrées dans la transcription ;
* ⚡ transcription native via MLX ;
* 🖥️ application de bureau Tauri.

⸻

Prérequis

Pour le développement :

* Node ≥ 24
* pnpm ≥ 12
* Python 3.11+
* Docker
* Rust / Cargo

Installer pnpm :

npm i -g pnpm

Aucun Homebrew n’est requis pour le worker ASR : MLX s’installe via pip et ne nécessite aucune compilation spécifique.

⸻

Démarrage

pnpm install
pnpm typecheck
pnpm test

⸻

App locale

./bench/.venv/bin/python apps/studio/server.py

Puis ouvrir :

http://127.0.0.1:8765

La version locale fonctionne sans Docker ni base de données.

Elle fournit notamment :

* bibliothèque par matière et chapitre ;
* recherche plein texte ;
* enregistrement micro ;
* transcription locale ;
* timestamps cliquables ;
* import d’images et de documents ;
* édition des notes ;
* KaTeX ;
* Mermaid ;
* export Markdown ;
* impression PDF.

Tout tourne localement sur la machine.

⸻

Interface

Le système visuel suit Hallmark :

npx skills add nutlope/hallmark

Principes :

* palette OKLCH avec une seule teinte d’ancrage ;
* Bricolage Grotesque + Cardo ;
* polices vendorisées localement ;
* grille d’espacement de 4 pt ;
* pas de sidebar permanente ;
* pas de dégradés.

Les polices sont servies depuis :

apps/studio/ui/vendor/fonts/

Aucun CDN n’est nécessaire : l’application reste utilisable sans réseau.

⸻

Modèles

La clé Mistral doit être placée dans un .env à la racine :

MISTRAL_API_KEY=...

Le fichier .env n’est pas suivi par Git.

Sans clé :

* la transcription continue de fonctionner ;
* la génération de notes peut basculer vers un modèle MLX local si celui-ci est disponible.

⸻

Validation des ancres

./bench/.venv/bin/python apps/studio/test_anchors.py

La suite couvre actuellement 11 cas autour de la validation des références entre notes générées et transcription.

Un bloc sans ancre valide est rejeté, pas simplement marqué comme incertain.

⸻

Application de bureau

cd apps/desktop/src-tauri
cargo build --release

Le paquet macOS est généré dans :

apps/desktop/dist-app/Amphi.app

L’adresse du serveur est configurée dans :

~/Library/Application Support/Amphi/server.txt

Les secrets ne sont jamais lus depuis des fichiers classiques.

Sur macOS, ajouter dans le trousseau Keychain avec le service Amphi :

server-token
server-password

Overrides explicites de développement :

AMPHI_TOKEN=
AMPHI_PASSWORD=

Sous Linux, aucun fichier de secrets n’est utilisé et l’application reste sans authentification tant qu’aucune variable d’environnement n’est fournie.

⸻

Pourquoi une app native

Mesuré sur la même machine et le même audio :

whisper-base / WebGPU     ≈ 3,9× temps réel
large-v3-turbo / WebGPU   ≈ 0,5× estimé
large-v3-turbo / MLX      ≈ 9,9× temps réel

Le moteur natif permet donc d’utiliser un modèle beaucoup plus lourd tout en restant largement au-dessus du temps réel.

Ce choix détermine notamment si un étudiant peut transcrire gratuitement sur sa propre machine ou s’il faut envoyer systématiquement l’audio vers un serveur.

⸻

Banc de mesure Whisper

cd bench
python3 -m venv .venv
./.venv/bin/pip install mlx-whisper
./.venv/bin/python bench_whisper.py

Le premier passage télécharge le modèle, environ 1,6 Go.

Le script mesure ensuite le régime à chaud utilisé par le worker et écrit :

bench/results/whisper-m4.json

Pour régénérer l’audio de test :

say -v Jacques \
  -f fixtures/cours-regularisation.txt \
  -o /tmp/cours.aiff
afconvert \
  -f WAVE \
  -d LEI16@16000 \
  -c 1 \
  /tmp/cours.aiff \
  fixtures/cours-regularisation.wav

L’audio étant synthétique et anormalement propre, le WER obtenu doit être considéré comme un plancher de validation, pas comme une prévision terrain.

La vraie mesure devra être faite sur un cours enregistré en conditions réelles.

⸻

Structure

apps/web          PWA Next.js — capture, lecture, édition
apps/api          API Fastify — REST + WebSocket de session
apps/realtime     Hocuspocus (Yjs) + présence
apps/worker       Jobs BullMQ — ASR, consensus, génération de notes
apps/mac-worker   Worker ASR local opportuniste (MLX), ADR-15
apps/studio       Application locale légère
apps/desktop      Application Tauri
packages/shared   Types, schémas Zod, interfaces fournisseurs
packages/db       Schéma Drizzle et migrations
packages/consensus Fusion multi-flux — TypeScript pur, sans I/O
bench             Whisper, WER, benchmarks et comparaison de modèles

⸻

Conventions

* TypeScript strict ;
* aucun any ;
* validation Zod à toutes les frontières ;
* fournisseurs externes ASR, LLM et stockage derrière des interfaces ;
* changer de fournisseur doit être un changement de configuration, pas une réécriture ;
* packages/consensus reste pur et sans I/O ;
* le consensus doit être testable sur des cas synthétiques et sur des séances rejouées ;
* un bloc de notes sans ancre valide vers la transcription est écarté.

⸻

Roadmap

* Capture audio
* Transcription Whisper
* Timestamps
* Notes générées
* Validation des ancres
* Import photos
* Import documents
* Notes éditables
* Mermaid
* Export Markdown
* Release desktop stable
* Multi-appareils
* Consensus multi-flux
* Affichage des désaccords
* Import ICS
* Recherche sémantique
* Flashcards
* Slides
* Exports supplémentaires

⸻

Contribution

Les contributions sont bienvenues.

Avant toute modification structurelle importante, lire :

ARCHITECTURE.md

Puis lancer :

pnpm typecheck
pnpm test

avant d’ouvrir une Pull Request.

⸻

GitHub Topics

À mettre dans About → Topics sur GitHub :

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

⸻

License

Voir LICENSE.

Là c’est bien le **source Markdown complet**, donc quand tu le colles dans `README.md`, GitHub rendra directement les titres, le gras, les tableaux, checkboxes, blocs de code et liens.
