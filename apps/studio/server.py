#!/usr/bin/env python3
"""
Amphi Studio — l'app locale, celle qu'on ouvre et sur laquelle on clique.

Sert l'interface, transcrit avec le moteur MLX local, et génère les notes.
Aucune base de données, aucun Docker, aucun compte : tout tourne sur ce Mac.
C'est le jalon M1 sous sa forme démontrable — l'API Fastify et Postgres servent
au déploiement multi-utilisateurs, pas à ça.

    ./bench/.venv/bin/python apps/studio/server.py
    → http://127.0.0.1:8765

La génération de notes a besoin d'une clé Mistral (MISTRAL_API_KEY). Sans elle,
la transcription fonctionne quand même — c'est la moitié qui n'a besoin de rien.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

STUDIO_DIR = Path(__file__).parent
sys.path.insert(0, str(STUDIO_DIR.parent / "mac-worker"))

from asr_server import (  # noqa: E402
    Handler as AsrHandler,
    Transcriber,
    decode_to_pcm,
    suffix_for,
    to_asr_result,
)

LOG = logging.getLogger("amphi.studio")

MISTRAL_URL = "https://api.mistral.ai/v1/chat/completions"
MISTRAL_MODEL = os.environ.get("AMPHI_LLM_MODEL", "mistral-small-latest")
LOCAL_LLM_MODEL = os.environ.get("AMPHI_LOCAL_LLM", "mlx-community/Qwen2.5-7B-Instruct-4bit")

# Tarifs §11.3, en millièmes d'euro par token.
EUR_PER_USD = 0.92
IN_MILLICENTS = 0.15 * EUR_PER_USD * 1000 / 1_000_000
OUT_MILLICENTS = 0.60 * EUR_PER_USD * 1000 / 1_000_000

SYSTEM_PROMPT = """Tu produis les notes de cours d'un étudiant à partir de la transcription d'une séance.

Tu reçois deux sortes de sources :
- la TRANSCRIPTION, découpée en segments numérotés [s0], [s1]… ;
- des DOCUMENTS numérotés [a0], [a1]… — photos du tableau, diapositives, notes collées.

RÈGLE ABSOLUE : chaque bloc que tu produis cite ses sources, dans `sourceSegmentIds`
pour la transcription ou dans `sourceAttachmentIds` pour les documents. Un bloc sans
source vérifiable est rejeté et n'apparaîtra pas dans les notes de l'étudiant. Quand
une photo du tableau confirme ou complète ce qui est dit à l'oral, cite les deux. Ne rédige donc rien qui ne soit dit dans la
transcription : pas de complément de culture générale, pas de reformulation qui
ajoute une information absente.

Écris en français, dans la langue du cours. Garde les termes techniques anglais tels
qu'ils sont prononcés. Structure hiérarchiquement. Extrais les définitions et les
formules. Signale ce que l'enseignant présente comme important ou comme un piège.

Réponds UNIQUEMENT par un objet JSON valide de cette forme :
{
  "title": "titre du cours",
  "summary": "deux ou trois phrases",
  "blocks": [
    {"type":"heading","level":2,"text":"...","sourceSegmentIds":["s0"]},
    {"type":"paragraph","text":"...","sourceSegmentIds":["s1","s2"]},
    {"type":"bullets","items":["...","..."],"sourceSegmentIds":["s3"]},
    {"type":"definition","term":"...","definition":"...","sourceSegmentIds":["s4"]},
    {"type":"callout","kind":"a-retenir","text":"...","sourceSegmentIds":["s5"]},
    {"type":"paragraph","text":"...","sourceAttachmentIds":["a0"]}
  ],
  "glossary": [{"term":"...","definition":"...","sourceSegmentIds":["s4"]}]
}
`kind` vaut a-retenir, exemple, attention ou question-ouverte."""

VISION_PROMPT = """Tu lis une photo prise pendant un cours : tableau, diapositive projetée, ou page de notes.

Restitue son contenu en markdown, fidèlement et sans rien inventer :
- les formules mathématiques en LaTeX entre $...$ ou $$...$$ ;
- les schémas et graphiques : décris-les en une ou deux phrases entre crochets, par exemple [Schéma : pipeline ETL, trois étapes reliées par des flèches] ;
- ce qui est illisible : écris [illisible] plutôt que de deviner.

Ne commente pas, ne résume pas, n'ajoute aucune explication : tu transcris."""

DIAGRAM_PROMPT = """Tu produis un diagramme Mermaid à partir d'un passage de cours.

Choisis le type qui convient au contenu : flowchart pour un processus ou un pipeline,
sequenceDiagram pour des échanges, classDiagram pour une structure, erDiagram pour un
modèle de données, gantt pour un planning. Ne force pas un flowchart sur ce qui n'en est pas un.

Contraintes de syntaxe, importantes car le rendu échoue sinon :
- mets tout libellé contenant des espaces, accents ou ponctuation entre guillemets ;
- pas de parenthèses ni de crochets nus dans les libellés ;
- huit à quinze nœuds au maximum, un diagramme illisible ne sert à rien.

Réponds uniquement par un objet JSON :
{"mermaid": "flowchart TD\\n  A[\\"...\\"] --> B[\\"...\\"]", "title": "titre court", "explanation": "une phrase sur ce que montre le schéma"}"""

STOPWORDS = {
    "le", "la", "les", "de", "des", "du", "un", "une", "et", "ou", "que", "qui",
    "dans", "pour", "sur", "avec", "est", "sont", "ce", "cette", "ces", "on",
    "il", "elle", "nous", "vous", "en", "au", "aux", "par", "pas", "plus", "a",
}


def content_words(text: str) -> set[str]:
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return {w for w in re.sub(r"[^a-z0-9\s]", " ", text).split() if len(w) > 3 and w not in STOPWORDS}


def block_text_of(block: dict[str, Any]) -> str:
    parts = [str(v) for k, v in block.items() if k in ("text", "term", "definition")]
    parts.extend(block.get("items", []) or [])
    return " ".join(parts)


def verify_anchor(
    block: dict[str, Any],
    segments: list[dict[str, Any]],
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """
    Vérification d'ancrage — §6.3.

    Un modèle bon marché cite volontiers des sources plausibles mais fausses. On
    contrôle donc deux choses : que les identifiants existent, et qu'il reste un
    recouvrement lexical entre le bloc et le texte cité. Sans ça, « traçable »
    ne voudrait rien dire de plus que « le modèle a écrit un numéro ».

    Deux natures de source coexistent depuis l'ajout des photos : un segment de
    transcription porte un horodatage cliquable, une pièce jointe n'en a pas.
    Le bloc doit être rattaché à l'une ou à l'autre — jamais à rien.
    """
    attachments = attachments or []

    # Piste pièce jointe : une photo de tableau n'a pas d'horodatage, mais elle
    # reste une source vérifiable.
    att_indices = []
    for aid in block.get("sourceAttachmentIds") or []:
        match = re.fullmatch(r"a(\d+)", str(aid).strip())
        if match and int(match.group(1)) < len(attachments):
            att_indices.append(int(match.group(1)))

    ids = block.get("sourceSegmentIds") or []
    indices = []
    for sid in ids:
        match = re.fullmatch(r"s(\d+)", str(sid).strip())
        if match and int(match.group(1)) < len(segments):
            indices.append(int(match.group(1)))

    if not indices and not att_indices:
        return None

    # Un bloc peut citer l'oral ET le tableau — c'est même le cas le plus utile,
    # quand la photo confirme une formule dictée. Le recouvrement se vérifie sur
    # l'union des sources, et l'ancre garde l'horodatage dès qu'il y en a un.
    cited = " ".join(
        [segments[i]["text"] for i in indices] + [attachments[i]["text"] for i in att_indices]
    )
    produced = content_words(block_text_of(block))
    source = content_words(cited)
    # Un titre court peut légitimement ne partager aucun mot plein : on ne
    # l'exige qu'au-delà de quelques mots de contenu.
    if len(produced) >= 4 and len(produced & source) == 0:
        return None

    names = [attachments[i]["name"] for i in att_indices]
    if not indices:
        return {"kind": "attachment", "attachmentIds": [f"a{i}" for i in att_indices], "names": names}
    return {
        "kind": "transcript",
        "segmentIds": [f"s{i}" for i in indices],
        "startMs": min(segments[i]["startMs"] for i in indices),
        "endMs": max(segments[i]["endMs"] for i in indices),
        "names": names,
    }


_local_llm: Any = None


def local_llm_available() -> bool:
    """Le modèle local est-il déjà téléchargé ? On ne déclenche pas le téléchargement ici."""
    from pathlib import Path as _P

    slug = LOCAL_LLM_MODEL.replace("/", "--")
    cache = _P.home() / ".cache" / "huggingface" / "hub" / f"models--{slug}"
    return cache.exists() and not any(cache.rglob("*.incomplete"))


def generate_local(transcript: str) -> tuple[dict[str, Any], dict[str, int], float]:
    """
    Génération sans compte ni clé — ADR-16, la moitié locale.

    Plus lent et moins fiable qu'un modèle hébergé sur la structure JSON, mais il
    ne dépend de personne : c'est ce qui permet à l'app de marcher le jour où la
    clé expire, où le plan n'est pas activé, ou simplement hors ligne.
    """
    global _local_llm
    from mlx_lm import generate, load

    if _local_llm is None:
        LOG.info("chargement du modèle local %s", LOCAL_LLM_MODEL)
        _local_llm = load(LOCAL_LLM_MODEL)
    model, tokenizer = _local_llm

    prompt = tokenizer.apply_chat_template(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": transcript},
        ],
        add_generation_prompt=True,
    )
    started = time.perf_counter()
    raw = generate(model, tokenizer, prompt=prompt, max_tokens=4096, verbose=False)
    latency_ms = (time.perf_counter() - started) * 1000

    # Les petits modèles encadrent volontiers leur JSON de balises markdown.
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("le modèle local n'a pas produit de JSON exploitable")

    return json.loads(text[start : end + 1]), {"prompt_tokens": 0, "completion_tokens": 0}, latency_ms


def mistral_chat(
    api_key: str, messages: list[dict[str, Any]], *, model: str | None = None, max_tokens: int = 6000
) -> tuple[dict[str, Any], dict[str, int], float]:
    """Appel générique. `messages` peut contenir du texte et des images."""
    body = json.dumps(
        {
            "model": model or MISTRAL_MODEL,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        MISTRAL_URL,
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=240) as response:
        payload = json.loads(response.read())
    latency_ms = (time.perf_counter() - started) * 1000
    content = payload["choices"][0]["message"]["content"]
    return json.loads(content), payload.get("usage", {}), latency_ms


def mistral_vision_text(api_key: str, data_url: str) -> tuple[str, dict[str, int], float]:
    """Photo du tableau ou de diapositive → markdown. Sortie libre, pas de JSON."""
    body = json.dumps(
        {
            "model": MISTRAL_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": VISION_PROMPT},
                        {"type": "image_url", "image_url": data_url},
                    ],
                }
            ],
            "temperature": 0.1,
            "max_tokens": 3000,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        MISTRAL_URL,
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=240) as response:
        payload = json.loads(response.read())
    latency_ms = (time.perf_counter() - started) * 1000
    return (
        payload["choices"][0]["message"]["content"].strip(),
        payload.get("usage", {}),
        latency_ms,
    )


def call_mistral(api_key: str, transcript: str) -> tuple[dict[str, Any], dict[str, int], float]:
    body = json.dumps(
        {
            "model": MISTRAL_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": transcript},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            "max_tokens": 6000,
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        MISTRAL_URL,
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=180) as response:
        payload = json.loads(response.read())
    latency_ms = (time.perf_counter() - started) * 1000

    content = payload["choices"][0]["message"]["content"]
    usage = payload.get("usage", {})
    return json.loads(content), usage, latency_ms


DATA_DIR = STUDIO_DIR.parent.parent / "data" / "studio"


class StudioHandler(AsrHandler):
    """Étend le serveur ASR : sert l'interface, les pièces jointes, les notes et les schémas."""

    def _read_json(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length)) if length else {}

    def _api_key(self) -> str:
        return os.environ.get("MISTRAL_API_KEY", "")

    # ------------------------------------------------------------------ GET

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/index.html"):
            self._send_file(STUDIO_DIR / "ui" / "index.html", "text/html; charset=utf-8")
            return
        if self.path == "/vendor/mermaid.min.js":
            self._send_file(STUDIO_DIR / "ui" / "vendor" / "mermaid.min.js", "application/javascript")
            return
        if self.path.startswith("/load"):
            doc_id = self.path.partition("?id=")[2] or "default"
            path = DATA_DIR / f"{re.sub(r'[^a-zA-Z0-9_-]', '', doc_id)}.json"
            self._send(200, json.loads(path.read_text("utf-8")) if path.exists() else {"empty": True})
            return
        super().do_GET()

    def _send_file(self, path: Path, content_type: str) -> None:
        if not path.exists():
            self._send(404, {"error": f"{path.name} introuvable"})
            return
        blob = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    # ----------------------------------------------------------------- POST

    def do_POST(self) -> None:  # noqa: N802
        routes = {
            "/notes": self.handle_notes,
            "/attachment": self.handle_attachment,
            "/diagram": self.handle_diagram,
            "/save": self.handle_save,
        }
        handler = routes.get(self.path)
        if handler is None:
            super().do_POST()
            return
        try:
            handler()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:250]
            hint = (
                " La clé est valide mais le plan du compte n'est pas actif : vérifie ton "
                "numéro sur console.mistral.ai."
                if exc.code == 429
                else ""
            )
            self._send(502, {"error": f"Mistral HTTP {exc.code} : {detail}{hint}"})
        except ValueError as exc:
            self._send(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            LOG.exception("échec sur %s", self.path)
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    # ---------------------------------------------------------- pièces jointes

    def handle_attachment(self) -> None:
        """
        Photo du tableau ou texte collé → source utilisable par la génération.

        Le texte extrait devient une source citable au même titre qu'un segment
        de transcription : un bloc de notes tiré d'une photo reste vérifiable,
        il renvoie simplement à l'image plutôt qu'à un horodatage.
        """
        payload = self._read_json()
        kind = payload.get("kind")
        name = str(payload.get("name") or "sans-nom")[:120]

        if kind == "text":
            text = str(payload.get("text") or "").strip()
            if text == "":
                raise ValueError("texte vide")
            self._send(200, {"kind": "text", "name": name, "text": text, "costEuros": "0,0000 €"})
            return

        if kind != "image":
            raise ValueError("kind doit valoir 'image' ou 'text'")

        data_url = str(payload.get("dataUrl") or "")
        if not data_url.startswith("data:image/"):
            raise ValueError("dataUrl doit être une image en base64")

        api_key = self._api_key()
        if api_key == "":
            raise ValueError("MISTRAL_API_KEY absente : la lecture des photos en a besoin")

        text, usage, latency_ms = mistral_vision_text(api_key, data_url)
        cost = usage.get("prompt_tokens", 0) * IN_MILLICENTS + usage.get("completion_tokens", 0) * OUT_MILLICENTS
        self._send(
            200,
            {
                "kind": "image",
                "name": name,
                "text": text,
                "costEuros": f"{cost / 1000:.4f} €",
                "latencyMs": round(latency_ms),
                "usage": usage,
            },
        )

    # ------------------------------------------------------------------ notes

    def handle_notes(self) -> None:
        payload = self._read_json()
        segments = payload.get("segments") or []
        attachments = payload.get("attachments") or []
        # §6.4 : une génération ne réécrit jamais ce qu'un humain a touché.
        keep = payload.get("keepEdited") or []

        if not segments and not attachments:
            raise ValueError("ni transcription ni pièce jointe : rien à résumer")

        parts = []
        if segments:
            parts.append("TRANSCRIPTION DE LA SÉANCE\n" + "\n".join(f"[s{i}] {s['text']}" for i, s in enumerate(segments)))
        if attachments:
            parts.append(
                "DOCUMENTS FOURNIS — photos du tableau, diapositives, notes collées\n"
                + "\n\n".join(f"[a{i}] {a['name']}\n{a['text']}" for i, a in enumerate(attachments))
            )
        if keep:
            parts.append(
                "BLOCS DÉJÀ RÉDIGÉS PAR L'ÉTUDIANT — ne les reprends pas, complète autour :\n"
                + "\n".join(f"- {b}" for b in keep[:40])
            )

        api_key = self._api_key()
        engine, problems = "aucun", []
        doc = usage = None
        latency_ms = 0.0

        if api_key != "":
            try:
                doc, usage, latency_ms = mistral_chat(
                    api_key,
                    [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": "\n\n".join(parts)},
                    ],
                )
                engine = MISTRAL_MODEL
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:200]
                problems.append(
                    "Mistral refuse l'inférence (429) : plan du compte inactif."
                    if exc.code == 429
                    else f"Mistral HTTP {exc.code} : {detail}"
                )
        else:
            problems.append("MISTRAL_API_KEY absente.")

        if doc is None:
            if not local_llm_available():
                self._send(503, {"error": " ".join(problems) + f" Modèle local ({LOCAL_LLM_MODEL}) pas encore téléchargé."})
                return
            doc, usage, latency_ms = generate_local("\n\n".join(parts))
            engine = LOCAL_LLM_MODEL.split("/")[-1] + " (local)"

        kept, rejected = [], 0
        for block in doc.get("blocks", []):
            anchor = verify_anchor(block, segments, attachments)
            if anchor is None:
                rejected += 1
                continue
            block["anchor"] = anchor
            kept.append(block)

        glossary = []
        for entry in doc.get("glossary", []):
            anchor = verify_anchor(entry, segments, attachments)
            if anchor is not None:
                entry["anchor"] = anchor
                glossary.append(entry)

        cost = usage.get("prompt_tokens", 0) * IN_MILLICENTS + usage.get("completion_tokens", 0) * OUT_MILLICENTS
        self._send(
            200,
            {
                "title": doc.get("title", "Séance"),
                "summary": doc.get("summary", ""),
                "blocks": kept,
                "glossary": glossary,
                "rejectedBlocks": rejected,
                "model": engine,
                "costEuros": "0,0000 € (local)" if usage.get("prompt_tokens", 0) == 0 else f"{cost / 1000:.4f} €",
                "latencyMs": round(latency_ms),
                "usage": usage,
                "fallbackNote": " ".join(problems) if problems and engine != MISTRAL_MODEL else None,
            },
        )

    # ---------------------------------------------------------------- schémas

    def handle_diagram(self) -> None:
        """Commande /diagram du §3.6 : un passage de cours → un Mermaid."""
        payload = self._read_json()
        context = str(payload.get("context") or "").strip()
        instruction = str(payload.get("instruction") or "").strip()
        if context == "":
            raise ValueError("aucun passage fourni")

        api_key = self._api_key()
        if api_key == "":
            raise ValueError("MISTRAL_API_KEY absente : la génération de schémas en a besoin")

        user = context if instruction == "" else f"Consigne : {instruction}\n\nPassage :\n{context}"
        doc, usage, latency_ms = mistral_chat(
            api_key,
            [{"role": "system", "content": DIAGRAM_PROMPT}, {"role": "user", "content": user}],
            max_tokens=1500,
        )
        cost = usage.get("prompt_tokens", 0) * IN_MILLICENTS + usage.get("completion_tokens", 0) * OUT_MILLICENTS
        self._send(
            200,
            {
                "mermaid": doc.get("mermaid", ""),
                "title": doc.get("title", "Schéma"),
                "explanation": doc.get("explanation", ""),
                "costEuros": f"{cost / 1000:.4f} €",
                "latencyMs": round(latency_ms),
            },
        )

    # ------------------------------------------------------------ persistance

    def handle_save(self) -> None:
        payload = self._read_json()
        doc_id = re.sub(r"[^a-zA-Z0-9_-]", "", str(payload.get("id") or "default")) or "default"
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        path = DATA_DIR / f"{doc_id}.json"
        # Écriture atomique : une sauvegarde interrompue ne doit pas laisser un
        # document tronqué à la place de notes de cours.
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)
        self._send(200, {"ok": True, "id": doc_id, "path": str(path)})


def load_dotenv() -> None:
    """Charge le .env de la racine du dépôt. Il n'est jamais suivi par git."""
    env_file = STUDIO_DIR.parent.parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line == "" or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    load_dotenv()
    port = int(os.environ.get("PORT", "8765"))
    model = os.environ.get("AMPHI_ASR_MODEL", "mlx-community/whisper-large-v3-turbo")

    StudioHandler.transcriber = Transcriber(model)
    LOG.info("chargement du modèle %s", model)
    StudioHandler.transcriber.warm_up()

    if os.environ.get("MISTRAL_API_KEY", "") == "":
        LOG.warning("MISTRAL_API_KEY absente — la transcription marchera, pas la génération de notes")

    server = ThreadingHTTPServer(("127.0.0.1", port), StudioHandler)
    LOG.info("Amphi Studio prêt → http://127.0.0.1:%d", port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
