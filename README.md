Amphi

<p align="center">
  <strong>Collaborative lecture transcription and source-backed AI notes.</strong>
</p>
<p align="center">
  Record lectures from multiple devices, merge the transcriptions, and generate structured notes where every claim links back to a timestamped source.
</p>

⸻

About

Amphi is an open-source lecture note-taking application designed for students.

Instead of relying on a single recording, several devices can record the same lecture. Amphi aligns and merges the different transcriptions into a cleaner canonical transcript, then uses an LLM to generate structured notes.

The important part: every generated claim must point back to a timestamped passage from the original lecture.

Multiple recordings
        ↓
Speech-to-text
        ↓
Transcript alignment
        ↓
Canonical transcript
        ↓
AI note generation
        ↓
Source validation
        ↓
Structured notes with timestamps

Amphi is designed around university lectures, amphitheatres and study groups.

⸻

Download

If you simply want to use Amphi, you do not need to clone or build the repository.

Go to the repository’s Releases section and download the latest version for your platform.

➜ Download Amphi from Releases

The desktop application is currently under active development, with macOS being the main development platform.

⸻

Features

* 🎙️ Lecture recording
* 🧠 Local Whisper transcription
* 👥 Multi-device recordings
* 🔀 Multi-transcript consensus
* 📝 AI-generated structured notes
* 🔗 Timestamped sources for generated information
* ▶️ Click a timestamp to return to the original lecture
* 🖼️ Import board photos
* 📄 Import documents
* ✏️ Editable notes
* ∑ LaTeX equations with KaTeX
* 📊 Mermaid diagrams
* 🔎 Full-text search
* 📤 Markdown export
* 🖨️ PDF printing
* 🔒 Local-first processing
* 🖥️ Native Tauri desktop application

⸻

Why Amphi?

Most AI note-taking tools essentially work like this:

Audio → AI summary

Amphi takes a different approach:

Audio
  ↓
Transcript
  ↓
Verified source
  ↓
Structured notes
  ↓
Timestamped evidence

The transcript remains the source of truth.

If a generated note cannot be supported by the cited transcript, it is rejected instead of displayed.

⸻

Principles

Nothing invented

A generated block whose cited source does not support its content is discarded.

No empty sections

If a lecturer announces a section but never explains it, Amphi does not create an empty section just because the title was mentioned.

External information stays visible

Amphi can optionally complete missing prerequisites or context.

Any information that does not come directly from the lecture is explicitly marked as external knowledge.

It is never silently mixed with the lecturer’s content.

⸻

Project status

Milestone	Description	Status
M0	Architecture, costs and technical decisions	✅ Complete
M1	Recording → ASR → anchored notes → timestamp playback	✅ Demonstrable
M1+	Documents, photos, editable notes and Mermaid	✅ Demonstrable
M2	Tauri desktop app + embedded native transcription	🚧 In progress
M3	Multi-device consensus and disagreement detection	📋 Planned
M4	ICS import and semantic search	📋 Planned
M5	Slides, flashcards and additional exports	📋 Planned

⸻

Performance

Native transcription is one of the main architectural choices behind Amphi.

Measured on an Apple M4:

Whisper large-v3-turbo
≈ 9.9× realtime using MLX

Benchmark:

bench/results/whisper-m4.json

Browser-based transcription was significantly slower during testing, which is one of the reasons Amphi is moving toward a native desktop application.

⸻

Cost target

Amphi is designed for a cohort rather than an expensive individual subscription.

Target workload:

10–30 students
~30 hours of lectures / week
≈ €2.80 / month

The final cost depends on usage, model providers and how much processing is performed locally.

⸻

Architecture

Detailed architectural decisions, costs, risks and milestones are documented here:

ARCHITECTURE.md

Read this before making significant architectural changes.

⸻

Development

Requirements

* Node.js 24+
* pnpm 12+
* Python 3.11+
* Docker
* Rust / Cargo for the desktop application

Install pnpm:

npm install -g pnpm

⸻

Setup

Clone the repository:

git clone https://github.com/YOUR_USERNAME/amphi.git
cd amphi

Install dependencies:

pnpm install

Run checks:

pnpm typecheck
pnpm test

⸻

Local Studio

Amphi Studio can run locally without Docker or a database.

./bench/.venv/bin/python apps/studio/server.py

Open:

http://127.0.0.1:8765

The local Studio currently supports:

* lecture library
* subject organisation
* chapters
* full-text search
* microphone recording
* local transcription
* clickable timestamps
* photo imports
* document imports
* editable notes
* KaTeX formulas
* Mermaid diagrams
* Markdown export
* PDF printing

Everything runs directly on the machine.

⸻

Environment variables

Create a .env file at the root of the project:

MISTRAL_API_KEY=your_key_here

The .env file is ignored by Git.

Without a Mistral API key, transcription still works.

When available, note generation can fall back to a local MLX model.

⸻

Anchor validation

Run the anchor validation tests with:

./bench/.venv/bin/python apps/studio/test_anchors.py

Generated note blocks without valid transcript references are rejected.

⸻

Desktop app

Build the Tauri application:

cd apps/desktop/src-tauri
cargo build --release

On macOS, the application is generated in:

apps/desktop/dist-app/Amphi.app

Server configuration:

~/Library/Application Support/Amphi/server.txt

Secrets are never stored in plain configuration files.

On macOS, credentials can be stored in Keychain using the Amphi service:

server-token
server-password

Development overrides:

AMPHI_TOKEN=
AMPHI_PASSWORD=

⸻

Whisper benchmark

Create the Python environment:

cd bench
python3 -m venv .venv
./.venv/bin/pip install mlx-whisper
./.venv/bin/python bench_whisper.py

The first run downloads the selected Whisper model.

Benchmark results are written to:

bench/results/whisper-m4.json

⸻

Generate benchmark audio

say -v Jacques \
  -f fixtures/cours-regularisation.txt \
  -o /tmp/cours.aiff
afconvert \
  -f WAVE \
  -d LEI16@16000 \
  -c 1 \
  /tmp/cours.aiff \
  fixtures/cours-regularisation.wav

Synthetic speech is considerably cleaner than an actual university lecture.

The resulting WER should therefore be treated as a validation baseline rather than an estimate of real-world accuracy.

⸻

Repository structure

apps/
├── web          PWA — recording, playback and editing
├── api          Fastify API — REST + session WebSocket
├── realtime     Hocuspocus / Yjs realtime collaboration
├── worker       BullMQ jobs — ASR, consensus and note generation
├── mac-worker   Local MLX ASR worker
├── studio       Lightweight local application
└── desktop      Tauri desktop application
packages/
├── shared       Types, Zod schemas and provider interfaces
├── db           Drizzle schema and migrations
└── consensus    Pure TypeScript multi-transcript fusion
bench/
└── Benchmarks, WER tests and model comparisons

⸻

Development conventions

* TypeScript strict mode
* No any
* Zod validation at system boundaries
* External ASR, LLM and storage providers are hidden behind interfaces
* packages/consensus contains no I/O
* Consensus logic must remain independently testable
* Generated notes without valid transcript anchors are rejected

⸻

Roadmap

* Local lecture recording
* Whisper transcription
* Timestamped transcript
* AI-generated notes
* Source anchors
* Photo imports
* Document imports
* Editable notes
* Mermaid diagrams
* Markdown export
* Native desktop release
* Multi-device recording
* Transcript consensus
* Disagreement detection
* ICS timetable import
* Semantic search
* Lecture slides integration
* Flashcards
* Additional export formats

⸻

Contributing

Contributions, bug reports and ideas are welcome.

If you want to contribute:

1. Fork the repository
2. Create a branch

git checkout -b feature/my-feature

3. Make your changes
4. Run the checks

pnpm typecheck
pnpm test

5. Open a Pull Request

For significant architectural changes, please read ARCHITECTURE.md first.

⸻

License

See the LICENSE file for details.

⸻

Topics

Suggested GitHub repository topics:

amphi
education
students
university
lecture-notes
note-taking
ai-notes
speech-to-text
transcription
whisper
mlx
llm
local-ai
tauri
typescript
rust
nextjs
collaboration
open-source
student-tools
productivity
edtech
artificial-intelligence
