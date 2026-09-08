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

{LANGUAGE_RULE} Garde les termes techniques dans la langue où ils sont prononcés :
un cours français qui dit « gradient boosting » garde « gradient boosting ». Structure hiérarchiquement. Extrais les définitions et les
formules. Signale ce que l'enseignant présente comme important ou comme un piège.

N'ÉCRIS PAS DE SECTION VIDE. Si l'enseignant annonce un titre sans rien développer
dessous — « Introduction au CPU et à la RAM » suivi d'autre chose — n'invente pas de
contenu et ne crée pas la section. Un plan avec des rubriques creuses est pire que pas
de plan : l'étudiant croit avoir des notes et n'a que des intitulés. Mieux vaut trois
sections denses que douze coquilles. De même, **ne fabrique pas de bloc à partir d'une phrase administrative**. « Des
questions ? », « on verra ça la semaine prochaine », « vous m'entendez au fond »,
« on reprend où on s'était arrêtés » ne sont pas du contenu de cours : ils ne
méritent ni encadré, ni paragraphe. Seule exception : une échéance, une salle
d'examen ou une consigne de rendu, qui valent un encadré.

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
- **six à douze nœuds**, jamais plus : au-delà c'est une liste déguisée, pas un schéma ;
- le `title` doit décrire ce que le schéma montre RÉELLEMENT. Si tu produis un
  enchaînement linéaire, ne l'intitule pas « choix entre A, B et C » — ce serait mentir
  sur le contenu. Un titre juste et modeste vaut mieux qu'un titre vendeur et faux ;
- si le passage ne se prête pas à un schéma — une simple énumération, une définition —
  renvoie {"mermaid": "", "title": "", "explanation": "ce passage ne se prête pas à un
  schéma : ..."} plutôt que de forcer un diagramme sans intérêt.

Réponds uniquement par un objet JSON :
{"mermaid": "flowchart TD\\n  A[\\"...\\"] --> B[\\"...\\"]", "title": "titre court", "explanation": "une phrase sur ce que montre le schéma"}"""

ENRICH_PROMPT = """Tu aides un étudiant dont le cours a laissé des trous.

On te donne la transcription d'une séance. Ton travail n'est PAS de la résumer — un
autre passage s'en charge — mais d'identifier ce que l'enseignant a supposé connu sans
l'expliquer, et de le combler. Un sigle lâché sans définition, un prérequis implicite,
une notation jamais introduite, un concept mentionné puis abandonné.

{LANGUAGE_RULE}

Deux à cinq blocs, pas plus, et uniquement là où il y a un vrai trou. Si le cours se
suffit à lui-même, renvoie une liste vide : mieux vaut ne rien ajouter que du remplissage.

Cas particulier, fréquent : la séance est très courte, ou l'enseignant n'a fait
qu'annoncer un sujet sans le traiter — « aujourd'hui, introduction au CPU et à la
RAM », puis plus rien. Alors **le sujet annoncé est lui-même le trou** : introduis-le
proprement, comme le ferait un manuel, en trois à cinq blocs. C'est exactement la
situation où l'étudiant a le plus besoin de toi.

Ajoute un schéma Mermaid seulement quand il éclaire vraiment — six à douze nœuds,
libellés entre guillemets. La plupart des compléments n'en ont pas besoin.

Réponds uniquement par :
{"enrichments":[{"title":"...","text":"...","why":"pourquoi ça manque au cours",
                 "mermaid":"flowchart TD\\n  A[\\"...\\"] --> B[\\"...\\"]"}]}
Le champ "mermaid" est facultatif."""

STOPWORDS = {
    "le", "la", "les", "de", "des", "du", "un", "une", "et", "ou", "que", "qui",
    "dans", "pour", "sur", "avec", "est", "sont", "ce", "cette", "ces", "on",
    "il", "elle", "nous", "vous", "en", "au", "aux", "par", "pas", "plus", "a",
}


def content_words(text: str) -> set[str]:
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return {w for w in re.sub(r"[^a-z0-9\s]", " ", text).split() if len(w) > 3 and w not in STOPWORDS}


LANGUAGE_RULES = {
    "fr": "Écris les notes en français.",
    "en": "Write the notes in English.",
    "es": "Escribe los apuntes en español.",
    "de": "Schreibe die Notizen auf Deutsch.",
    "it": "Scrivi gli appunti in italiano.",
}


def resolve_language(requested: str, segments: list[dict[str, Any]]) -> tuple[str, str]:
    """
    Langue des notes : celle du cours, pas celle du développeur.

    Le prompt imposait « écris en français », ce qui produisait des notes
    françaises sur un cours anglophone. La langue détectée par Whisper est la
    source de vérité par défaut, et l'utilisateur peut toujours forcer.
    """
    code = (requested or "auto").lower()
    if code == "auto":
        votes: dict[str, int] = {}
        for seg in segments:
            lang = (seg.get("lang") or "").lower()[:2]
            if lang:
                votes[lang] = votes.get(lang, 0) + 1
        code = max(votes, key=votes.__getitem__) if votes else "fr"
    rule = LANGUAGE_RULES.get(code)
    if rule is None:
        rule = f"Write the notes in the same language as the course (code: {code})."
    return code, rule


def drop_hollow_headings(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Supprime les titres qui ne sont suivis d'aucun contenu.

    Le modèle a beau être prié de ne pas créer de section vide, il le fait quand
    l'enseignant annonce un plan qu'il ne développe pas. Un contrôle structurel
    est plus fiable qu'une consigne : un titre suivi d'un autre titre, ou en
    dernière position, ne sert à rien.
    """
    out: list[dict[str, Any]] = []
    for i, block in enumerate(blocks):
        if block.get("type") != "heading":
            out.append(block)
            continue
        nxt = next((b for b in blocks[i + 1 :] if b.get("type") != "heading"), None)
        following_heading = next((b for b in blocks[i + 1 :]), None)
        if nxt is None or (following_heading is not None and following_heading.get("type") == "heading"):
            continue
        out.append(block)
    return out


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


def generate_local(transcript: str, language_rule: str = "Écris les notes en français.") -> tuple[dict[str, Any], dict[str, int], float]:
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
            {"role": "system", "content": SYSTEM_PROMPT.replace("{LANGUAGE_RULE}", language_rule)},
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


def generate_enrichments(
    api_key: str, transcript: str, language_rule: str
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """
    Second appel, dédié aux compléments hors cours.

    Le faire dans le même appel que les notes ne marche pas : « n'invente rien,
    n'écris pas de section creuse » et « ajoute ce qui manque » sont deux
    consignes contradictoires, et le modèle finit par n'obéir qu'à la première.
    Deux appels, deux métiers. Le surcoût est de l'ordre du millième d'euro.
    """
    try:
        doc, usage, _ = mistral_chat(
            api_key,
            [
                {"role": "system", "content": ENRICH_PROMPT.replace("{LANGUAGE_RULE}", language_rule)},
                {"role": "user", "content": transcript},
            ],
            max_tokens=2500,
        )
    except Exception:  # noqa: BLE001 — un complément raté ne doit pas perdre les notes
        LOG.exception("génération des compléments")
        return [], {}
    # Le modèle renvoie tantôt {"enrichments":[...]}, tantôt le tableau nu.
    # Accepter les deux coûte trois lignes ; l'imposer coûte des générations perdues.
    if isinstance(doc, list):
        out = doc
    else:
        out = doc.get("enrichments") or doc.get("enrichment") or []
    return [e for e in out if isinstance(e, dict) and (e.get("text") or "").strip()][:5], usage


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


def safe_id(raw: Any) -> str:
    """Un identifiant de document ne doit jamais pouvoir sortir de DATA_DIR."""
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "", str(raw or ""))[:64]
    return cleaned or "sans-titre"


def list_documents() -> list[dict[str, Any]]:
    """Index de la bibliothèque : on lit l'en-tête de chaque fichier, pas tout le corps."""
    if not DATA_DIR.exists():
        return []
    docs = []
    for path in sorted(DATA_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            payload = json.loads(path.read_text("utf-8"))
        except (ValueError, OSError):
            continue
        doc = payload.get("doc") or {}
        docs.append(
            {
                "id": path.stem,
                "title": doc.get("title") or payload.get("title") or path.stem,
                "savedAt": payload.get("savedAt"),
                "blocks": len(doc.get("blocks") or []),
                "diagrams": len(payload.get("diagrams") or []),
                "attachments": len(payload.get("attachments") or []),
                "durationMs": (payload.get("segments") or [{}])[-1].get("endMs", 0) if payload.get("segments") else 0,
                "courseHint": payload.get("courseHint"),
            }
        )
    return docs


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
        if self.path.startswith("/vendor/"):
            # Ressources vendorisées : polices, mermaid. Jamais de CDN — l'app doit
            # rester utilisable dans un amphi au réseau douteux.
            rel = self.path[len("/vendor/"):]
            target = (STUDIO_DIR / "ui" / "vendor" / rel).resolve()
            root = (STUDIO_DIR / "ui" / "vendor").resolve()
            if root not in target.parents and target != root:
                self._send(403, {"error": "chemin hors du dossier vendor"})
                return
            types = {".js": "application/javascript", ".css": "text/css; charset=utf-8",
                     ".woff2": "font/woff2", ".woff": "font/woff"}
            self._send_file(target, types.get(target.suffix, "application/octet-stream"))
            return
        if self.path == "/docs":
            self._send(200, {"docs": list_documents()})
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
            "/delete": self.handle_delete,
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
        enrich = bool(payload.get("enrich"))
        lang_code, lang_rule = resolve_language(payload.get("language", "auto"), segments)
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
                        {"role": "system", "content": SYSTEM_PROMPT.replace("{LANGUAGE_RULE}", lang_rule)},
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
            doc, usage, latency_ms = generate_local("\n\n".join(parts), lang_rule)
            engine = LOCAL_LLM_MODEL.split("/")[-1] + " (local)"

        kept, rejected, enrichments = [], 0, []
        for block in doc.get("blocks", []):
            # Un bloc d'enrichissement assume de ne pas venir du cours : il n'a pas
            # d'ancre, et l'interface le présente à part. La garantie de traçabilité
            # devient « tout est soit ancré au cours, soit signalé comme extérieur ».
            if block.get("type") == "enrichment":
                if enrich and (block.get("text") or "").strip():
                    enrichments.append(block)
                continue
            anchor = verify_anchor(block, segments, attachments)
            if anchor is None:
                rejected += 1
                continue
            block["anchor"] = anchor
            kept.append(block)

        # Le modèle place volontiers l'enrichissement dans une clé de premier
        # niveau plutôt que parmi les blocs. On accepte les deux formes : imposer
        # une seule façon de répondre à un petit modèle, c'est perdre du contenu
        # valide pour une question de forme.
        # Les compléments viennent d'un appel séparé (voir generate_enrichments).
        # On accepte aussi la forme en ligne, au cas où le modèle en glisse.
        if enrich:
            for extra in doc.get("enrichments") or doc.get("enrichment") or []:
                if isinstance(extra, dict) and (extra.get("text") or "").strip():
                    enrichments.append(extra)
            if api_key != "":
                extra_blocks, extra_usage = generate_enrichments(api_key, "\n\n".join(parts), lang_rule)
                enrichments.extend(extra_blocks)
                # Le second appel compte dans la facture : l'afficher à part
                # reviendrait à sous-estimer le coût réel d'une génération.
                usage = {
                    "prompt_tokens": usage.get("prompt_tokens", 0) + extra_usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0) + extra_usage.get("completion_tokens", 0),
                }

        kept = drop_hollow_headings(kept)

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
                "enrichments": enrichments,
                "glossary": glossary,
                "rejectedBlocks": rejected,
                "language": lang_code,
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

        lang_rule = LANGUAGE_RULES.get(str(payload.get("language") or "fr")[:2], LANGUAGE_RULES["fr"])
        prefix = f"{lang_rule} Les libellés du schéma doivent être dans cette langue.\n\n"
        user = prefix + (context if instruction == "" else f"Consigne : {instruction}\n\nPassage :\n{context}")
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

    def handle_delete(self) -> None:
        payload = self._read_json()
        doc_id = safe_id(payload.get("id"))
        path = DATA_DIR / f"{doc_id}.json"
        path.unlink(missing_ok=True)
        self._send(200, {"ok": True, "id": doc_id})

    def handle_save(self) -> None:
        payload = self._read_json()
        doc_id = safe_id(payload.get("id"))
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
