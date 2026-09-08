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

La transcription est découpée en segments numérotés [s0], [s1], etc. RÈGLE ABSOLUE :
chaque bloc que tu produis doit citer dans `sourceSegmentIds` les identifiants des
segments dont il est tiré. Un bloc sans source vérifiable est rejeté et n'apparaîtra
pas dans les notes de l'étudiant. Ne rédige donc rien qui ne soit dit dans la
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
    {"type":"callout","kind":"a-retenir","text":"...","sourceSegmentIds":["s5"]}
  ],
  "glossary": [{"term":"...","definition":"...","sourceSegmentIds":["s4"]}]
}
`kind` vaut a-retenir, exemple, attention ou question-ouverte."""

STOPWORDS = {
    "le", "la", "les", "de", "des", "du", "un", "une", "et", "ou", "que", "qui",
    "dans", "pour", "sur", "avec", "est", "sont", "ce", "cette", "ces", "on",
    "il", "elle", "nous", "vous", "en", "au", "aux", "par", "pas", "plus", "a",
}


def content_words(text: str) -> set[str]:
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return {w for w in re.sub(r"[^a-z0-9\s]", " ", text).split() if len(w) > 3 and w not in STOPWORDS}


def verify_anchor(block: dict[str, Any], segments: list[dict[str, Any]]) -> dict[str, Any] | None:
    """
    Vérification d'ancrage — §6.3.

    Un modèle bon marché cite volontiers des segments plausibles mais faux. On
    contrôle donc deux choses : que les identifiants existent, et qu'il reste un
    recouvrement lexical entre le bloc et le texte cité. Sans ça, « traçable »
    ne voudrait rien dire de plus que « le modèle a écrit un numéro ».
    """
    ids = block.get("sourceSegmentIds") or []
    indices = []
    for sid in ids:
        match = re.fullmatch(r"s(\d+)", str(sid).strip())
        if match and int(match.group(1)) < len(segments):
            indices.append(int(match.group(1)))
    if not indices:
        return None

    cited = " ".join(segments[i]["text"] for i in indices)
    block_text = " ".join(
        str(v) for k, v in block.items() if k in ("text", "term", "definition")
    ) + " ".join(block.get("items", []))

    produced = content_words(block_text)
    source = content_words(cited)
    # Un titre court peut légitimement ne partager aucun mot plein : on ne
    # l'exige qu'au-delà de quelques mots de contenu.
    if len(produced) >= 4 and len(produced & source) == 0:
        return None

    return {
        "segmentIds": [f"s{i}" for i in indices],
        "startMs": min(segments[i]["startMs"] for i in indices),
        "endMs": max(segments[i]["endMs"] for i in indices),
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


class StudioHandler(AsrHandler):
    """Étend le serveur ASR : sert l'interface et ajoute la génération de notes."""

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/index.html"):
            html = (STUDIO_DIR / "ui" / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/notes":
            super().do_POST()
            return

        length = int(self.headers.get("Content-Length") or 0)
        try:
            segments = json.loads(self.rfile.read(length))["segments"]
        except (ValueError, KeyError) as exc:
            self._send(400, {"error": f"corps invalide : {exc}"})
            return

        if not segments:
            self._send(400, {"error": "aucun segment à résumer"})
            return

        transcript = "\n".join(f"[s{i}] {s['text']}" for i, s in enumerate(segments))

        # Ordre de préférence : Mistral s'il répond, sinon le modèle local. Un 429
        # de Mistral n'est pas une panne du produit — c'est un basculement.
        api_key = os.environ.get("MISTRAL_API_KEY", "")
        engine = "aucun"
        doc = usage = None
        latency_ms = 0.0
        problems: list[str] = []

        if api_key != "":
            try:
                doc, usage, latency_ms = call_mistral(api_key, transcript)
                engine = MISTRAL_MODEL
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:200]
                if exc.code == 429:
                    problems.append(
                        "Mistral refuse l'inférence (429). La clé est valide mais le plan "
                        "n'est pas actif : vérifie ton numéro de téléphone sur console.mistral.ai."
                    )
                else:
                    problems.append(f"Mistral HTTP {exc.code} : {detail}")
            except Exception as exc:  # noqa: BLE001
                problems.append(f"Mistral : {type(exc).__name__} {exc}")
        else:
            problems.append("MISTRAL_API_KEY absente.")

        if doc is None:
            if not local_llm_available():
                self._send(
                    503,
                    {
                        "error": " ".join(problems)
                        + f" Et le modèle local ({LOCAL_LLM_MODEL}) n'est pas encore téléchargé."
                    },
                )
                return
            try:
                doc, usage, latency_ms = generate_local(transcript)
                engine = LOCAL_LLM_MODEL.split("/")[-1] + " (local)"
            except Exception as exc:  # noqa: BLE001
                LOG.exception("génération locale")
                self._send(500, {"error": " ".join(problems) + f" Modèle local : {exc}"})
                return

        # Ancrage vérifié bloc par bloc. Ce qui ne passe pas n'est pas affiché.
        kept, rejected = [], 0
        for block in doc.get("blocks", []):
            anchor = verify_anchor(block, segments)
            if anchor is None:
                rejected += 1
                continue
            block["anchor"] = anchor
            kept.append(block)

        glossary = []
        for entry in doc.get("glossary", []):
            anchor = verify_anchor(entry, segments)
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
                "fallbackNote": " ".join(problems) if problems and engine != MISTRAL_MODEL else None,
                "latencyMs": round(latency_ms),
                "usage": usage,
            },
        )


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
