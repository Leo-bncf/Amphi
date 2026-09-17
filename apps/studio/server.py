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

import base64
import binascii
import html
import hmac
import io
import json
import logging
from datetime import datetime, timedelta, timezone
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlparse
import threading

import auth as identity_auth
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

STUDIO_DIR = Path(__file__).parent
sys.path.insert(0, str(STUDIO_DIR.parent / "mac-worker"))

# Le moteur de transcription tire numpy, MLX et ffmpeg. Sur la machine qui
# héberge l'app pour la promo — un Tinker Board, un Pi — rien de tout ça n'est
# installable ni utile : elle sert des pages et appelle des API, la
# transcription se fait ailleurs. L'import est donc facultatif.
# pypdf est du Python pur : il s'installe partout, y compris sur la carte ARM,
# sans compilation. Facultatif quand même — sans lui tout le reste fonctionne,
# seul le dépôt d'un PDF le réclame.
try:
    from pypdf import PdfReader  # noqa: E402

    PDF_AVAILABLE = True
except ImportError:  # pragma: no cover - dépend de la machine
    PDF_AVAILABLE = False

try:
    from asr_server import (  # noqa: E402
        Handler as AsrHandler,
        Transcriber,
        decode_to_pcm,  # noqa: F401  (utilisé par AsrHandler)
        suffix_for,  # noqa: F401
        to_asr_result,  # noqa: F401
    )

    ASR_AVAILABLE = True
except ImportError as exc:  # pragma: no cover - dépend de la machine
    ASR_AVAILABLE = False
    ASR_IMPORT_ERROR = str(exc)

    from http.server import BaseHTTPRequestHandler

    class AsrHandler(BaseHTTPRequestHandler):  # type: ignore[no-redef]
        """Serveur sans moteur local : tout marche sauf /transcribe."""

        transcriber = None
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            LOG.debug(fmt, *args)

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                self._send(200, {"ok": True, "service": "amphi-studio", "available": False,
                                 "warm": False, "model": "aucun moteur local", "batteryPercent": None,
                                 "reason": ASR_IMPORT_ERROR, **health_diagnostics()})
                return
            self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            self._send(
                503,
                {"error": "Pas de moteur de transcription sur cette machine. "
                          "Enregistre depuis un poste équipé, ou configure le repli payant."},
            )

    Transcriber = None  # type: ignore[assignment,misc]

LOG = logging.getLogger("amphi.studio")

# Mot de passe partagé. Absent, le service refuse de s'ouvrir sur le réseau :
# une URL publique sans verrou donne lecture ET suppression de toutes les notes
# à qui la connaît, et on ne remarque rien tant que quelqu'un n'a pas effacé.
AMPHI_PASSWORD = ""
REALM = "Amphi"

# Small, explicit wire contract for desktop clients. Keep this independent from
# Whisper/model versions: the hosted server must be able to reject stale clients
# before they start a recording or mutate a session.
COMPATIBILITY_VERSION = 1
SERVER_VERSION = os.environ.get("AMPHI_SERVER_VERSION", "0.1.0")
MIN_CLIENT_COMPATIBILITY_VERSION = int(os.environ.get("AMPHI_MIN_CLIENT_COMPATIBILITY", "1"))

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
    {"type":"formula","latex":"J(\\\\beta) = \\\\sum_i (y_i - \\\\hat{y}_i)^2 + \\\\lambda \\\\sum_j \\\\beta_j^2",
     "caption":"ce que la formule calcule","sourceSegmentIds":["s6"]},
    {"type":"paragraph","text":"...","sourceAttachmentIds":["a0"]}
  ],
  "glossary": [{"term":"...","definition":"...","sourceSegmentIds":["s4"]}]
}
`kind` vaut a-retenir, exemple, attention ou question-ouverte.

FORMULES. Dès que l'enseignant dicte une expression mathématique, produis un bloc
"formula" en LaTeX — pas une phrase qui la décrit. « la somme des carrés des résidus
plus lambda fois la somme des bêta j au carré » devient du LaTeX, jamais du texte.
Dans les autres blocs, les symboles et expressions courtes s'écrivent entre $...$ :
« le paramètre $\\lambda$ contrôle la pénalité ». C'est ce qui rend les notes
relisibles la veille d'un partiel."""

VISION_PROMPT = """Tu lis une photo prise pendant un cours : tableau, diapositive projetée,
page d'un polycopié, ou notes manuscrites prises à la main.

Restitue son contenu en markdown, fidèlement et sans rien inventer :
- les formules mathématiques en LaTeX entre $...$ ou $$...$$ ;
- les schémas et graphiques : décris-les en une ou deux phrases entre crochets, par exemple [Schéma : pipeline ETL, trois étapes reliées par des flèches] ;
- ce qui est illisible : écris [illisible] plutôt que de deviner.

Si ce sont des NOTES MANUSCRITES, restitue-les telles qu'elles sont écrites,
abréviations et flèches comprises : c'est la trace de ce que l'étudiant a jugé
important, elle vaut mieux qu'une reformulation propre. Garde la structure
visuelle — ce qui est encadré ou souligné dans le cahier l'était pour une raison.

Ne commente pas, ne résume pas, n'ajoute aucune explication : tu transcris."""

DIAGRAM_PROMPT = """Tu produis un diagramme Mermaid à partir d'un passage de cours.

DEUX TYPES AUTORISÉS, ET DEUX SEULEMENT : `flowchart` (processus, dépendances,
classification, structure — c'est le cas général) et `sequenceDiagram` (échanges entre
acteurs). N'utilise JAMAIS classDiagram, erDiagram, gantt, stateDiagram ni mindmap :
leurs libellés ne peuvent pas être protégés par des guillemets, et la moindre
ponctuation — un deux-points, une accolade, une parenthèse — casse le rendu chez
l'étudiant. Une structure conceptuelle se représente très bien en flowchart, avec un
nœud par notion et des flèches nommées.

Contraintes de syntaxe, importantes car le rendu échoue sinon :
- mets TOUT libellé entre guillemets doubles, sans exception : A["Bus de données"] ;
- pas de guillemet double À L'INTÉRIEUR d'un libellé ;
- pas de parenthèses ni de crochets nus hors des guillemets ;
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


# Mots-outils : ils sont fréquents, courts, et propres à chaque langue. Le
# vocabulaire technique, lui, voyage — un cours français dit « gradient
# boosting ». C'est donc la grammaire qui trahit la langue, pas le sujet.
LANG_STOPWORDS = {
    "fr": {"le", "la", "les", "des", "une", "est", "que", "qui", "pour", "dans", "sur", "avec", "pas", "plus", "cette", "sont"},
    "en": {"the", "and", "of", "to", "is", "that", "for", "with", "this", "are", "we", "can", "from", "which", "be", "it"},
    "es": {"el", "la", "los", "las", "una", "que", "para", "con", "por", "del", "es", "son", "como", "más"},
    "de": {"der", "die", "das", "und", "ist", "ein", "eine", "mit", "auf", "für", "nicht", "wir", "sind", "auch"},
    "it": {"il", "la", "le", "che", "per", "con", "una", "sono", "del", "della", "come", "più", "questo"},
}


def guess_language_from_text(text: str) -> str | None:
    """Langue d'un texte court, par vote de mots-outils. None si trop peu de signal."""
    words = re.findall(r"[a-zà-öø-ÿ]+", (text or "").lower())
    if len(words) < 25:
        return None
    votes = {code: sum(1 for w in words if w in stop) for code, stop in LANG_STOPWORDS.items()}
    best = max(votes, key=votes.__getitem__)
    # Un seul mot-outil reconnu sur cent, c'est du bruit, pas une langue.
    return best if votes[best] >= max(3, len(words) // 40) else None


def resolve_language(
    requested: str,
    segments: list[dict[str, Any]],
    attachments: list[dict[str, Any]] | None = None,
) -> tuple[str, str]:
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
        if votes:
            code = max(votes, key=votes.__getitem__)
        else:
            # Séance sans enregistrement : la langue se lit dans les photos du
            # tableau. Sans ça, un cours anglais photographié ressort en
            # français — la faute exacte qu'on a déjà corrigée pour l'audio.
            joined = " ".join(str(a.get("text") or "") for a in (attachments or []))
            code = guess_language_from_text(joined) or "fr"
    rule = LANGUAGE_RULES.get(code)
    if rule is None:
        rule = f"Write the notes in the same language as the course (code: {code})."
    return code, rule


def searchable_parts(payload: dict[str, Any]) -> list[tuple[str, str]]:
    """
    Décompose un document en (origine, texte) cherchables.

    On indexe aussi la transcription : c'est souvent là que se trouve ce dont on
    se souvient — « le prof a parlé de je-ne-sais-quoi » — alors que les notes,
    elles, ont reformulé.
    """
    doc = payload.get("doc") or {}
    out: list[tuple[str, str]] = [("titre", doc.get("title", "")), ("résumé", doc.get("summary", ""))]
    for block in doc.get("blocks") or []:
        text = " ".join(
            str(block.get(k, "")) for k in ("text", "term", "definition", "caption", "latex")
        ) + " ".join(block.get("items") or [])
        if text.strip():
            out.append(("notes", text))
    for entry in doc.get("glossary") or []:
        out.append(("glossaire", f"{entry.get('term','')} — {entry.get('definition','')}"))
    for extra in doc.get("enrichments") or []:
        out.append(("complément", f"{extra.get('title','')} {extra.get('text','')}"))
    for att in payload.get("attachments") or []:
        out.append(("document", att.get("text", "")))
    transcript = " ".join(seg.get("text", "") for seg in payload.get("segments") or [])
    if transcript.strip():
        out.append(("transcription", transcript))
    return out


def search_documents(query: str, limit: int = 60) -> list[dict[str, Any]]:
    """Recherche plein texte, insensible à la casse et aux accents."""
    needle = " ".join(w for w in content_words(query) if w) or query.strip().lower()
    if not needle:
        return []
    terms = needle.split()
    results = []
    for path in sorted(DATA_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            payload = json.loads(path.read_text("utf-8"))
        except (ValueError, OSError):
            continue
        hits, score = [], 0
        for origin, text in searchable_parts(payload):
            flat = strip_accents(text)
            for term in terms:
                pos = flat.find(term)
                if pos == -1:
                    continue
                score += 3 if origin in ("titre", "notes", "glossaire") else 1
                if len(hits) < 3:
                    start = max(0, pos - 55)
                    snippet = text[start : pos + len(term) + 85].strip()
                    hits.append({"where": origin, "snippet": ("…" if start else "") + snippet + "…"})
                break
        if hits:
            doc = payload.get("doc") or {}
            results.append({
                "id": path.stem,
                "title": doc.get("title") or path.stem,
                "course": (payload.get("course") or "").strip(),
                "chapter": (payload.get("chapter") or "").strip(),
                "savedAt": payload.get("savedAt"),
                "score": score,
                "hits": hits,
            })
    results.sort(key=lambda r: -r["score"])
    return results[:limit]


def strip_accents(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in text if unicodedata.category(c) != "Mn")


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
    parts = [str(v) for k, v in block.items() if k in ("text", "term", "definition", "caption")]
    parts.extend(block.get("items", []) or [])
    return " ".join(parts)


def _anchor_from(
    indices: list[int], att_indices: list[int],
    segments: list[dict[str, Any]], attachments: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Construit l'ancre une fois la vérification passée."""
    names = [attachments[i]["name"] for i in att_indices]
    if not indices:
        if not att_indices:
            return None
        return {"kind": "attachment", "attachmentIds": [f"a{i}" for i in att_indices], "names": names}
    return {
        "kind": "transcript",
        "segmentIds": [f"s{i}" for i in indices],
        "startMs": min(segments[i]["startMs"] for i in indices),
        "endMs": max(segments[i]["endMs"] for i in indices),
        "names": names,
    }


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
    attachments = [a for a in (attachments or []) if isinstance(a, dict)]

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
    # Une formule ne partage presque jamais de mots avec l'oral : « bêta j au
    # carré » prononcé devient \beta_j^2 écrit. Exiger un recouvrement lexical
    # supprimerait justement les blocs les plus utiles.
    if block.get("type") == "formula":
        return _anchor_from(indices, att_indices, segments, attachments)
    # Un titre court peut légitimement ne partager aucun mot plein : on ne
    # l'exige qu'au-delà de quelques mots de contenu.
    if block.get("type") != "formula" and len(produced) >= 4 and len(produced & source) == 0:
        return None

    return _anchor_from(indices, att_indices, segments, attachments)


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


def loads_lenient(raw: str) -> Any:
    """
    Parse une réponse JSON éventuellement tronquée.

    Quand le modèle atteint son plafond de tokens, il s'arrête au milieu d'un
    objet et tout l'appel est perdu. Plutôt que de rendre zéro bloc — ce qui
    donne « aucune note générée » sans explication — on récupère les éléments
    complets et on jette le dernier, incomplet.
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    # On recule jusqu'au dernier objet complet, puis on referme les structures.
    for cut in range(len(text) - 1, 0, -1):
        if text[cut] != "}":
            continue
        candidate = text[: cut + 1]
        for suffix in ("]}", "}]}", "}", "]", ""):
            try:
                return json.loads(candidate + suffix)
            except json.JSONDecodeError:
                continue
    raise ValueError("réponse du modèle illisible, même après récupération partielle")


# Le compte est plafonné à 100 000 tokens et 100 requêtes par minute. Générer
# les notes d'un cours d'une heure, c'est cinq ou six fenêtres tirées à la
# suite : le plafond se touche pour de bon, et il se relâche tout seul.
MISTRAL_RETRIES = 4


def mistral_error_text(exc: urllib.error.HTTPError) -> str:
    """Ce que Mistral a réellement répondu — pas notre supposition."""
    raw = exc.read().decode("utf-8", "replace")[:400]
    try:
        detail = json.loads(raw)
        message = detail.get("message") or (detail.get("error") or {}).get("message") or raw
    except (ValueError, AttributeError):
        message = raw
    if exc.code == 429:
        return (f"Mistral limite le débit (429 : {message}). Les reprises automatiques "
                "n'ont pas suffi — laisse passer une minute et relance la génération.")
    if exc.code in (401, 403):
        return f"Mistral refuse la clé (HTTP {exc.code} : {message})."
    return f"Mistral HTTP {exc.code} : {message}"


def mistral_post(api_key: str, payload: dict[str, Any], *, timeout: int = 240) -> tuple[dict[str, Any], float]:
    """
    Appel à Mistral, avec reprise sur les erreurs passagères.

    Deux familles d'erreurs passagères, traitées pareil. Le 429 : le débit par
    minute qu'on vient de dépasser, qui se relâche tout seul. La coupure
    réseau : un wifi qui bascule, un résolveur DNS qui ne répond pas une
    seconde. Aucune des deux ne justifie de perdre les fenêtres déjà générées
    d'un cours d'une heure.
    """
    body = json.dumps(payload).encode("utf-8")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    started = time.perf_counter()
    delay = 5.0
    for attempt in range(1, MISTRAL_RETRIES + 1):
        request = urllib.request.Request(MISTRAL_URL, data=body, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read()), (time.perf_counter() - started) * 1000
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == MISTRAL_RETRIES:
                raise
            # Mistral dit parfois lui-même combien de temps attendre ; sinon on
            # double à chaque fois, plafonné pour ne pas bloquer l'interface.
            header = exc.headers.get("Retry-After") if exc.headers else None
            try:
                wait = float(header) if header else delay
            except ValueError:
                wait = delay
            wait = min(max(wait, 1.0), 30.0)
            LOG.warning("Mistral %d — reprise dans %.0f s (tentative %d/%d)",
                        exc.code, wait, attempt, MISTRAL_RETRIES)
            time.sleep(wait)
            delay = min(delay * 2, 30.0)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # DNS muet, wifi qui bascule, machine qui sort de veille : ça se
            # rétablit en quelques secondes. Le message brut — « nodename nor
            # servname provided » — n'apprend rien à un étudiant.
            if attempt == MISTRAL_RETRIES:
                raise ConnectionError(
                    "Pas de connexion à Mistral (réseau ou DNS). Vérifie le wifi, "
                    "puis relance la génération — rien n'est perdu."
                ) from exc
            LOG.warning("réseau indisponible (%s) — reprise dans %.0f s (tentative %d/%d)",
                        type(exc).__name__, delay, attempt, MISTRAL_RETRIES)
            time.sleep(delay)
            delay = min(delay * 2, 30.0)
    raise RuntimeError("boucle de reprise sortie sans résultat")


def mistral_chat(
    api_key: str, messages: list[dict[str, Any]], *, model: str | None = None, max_tokens: int = 6000
) -> tuple[dict[str, Any], dict[str, int], float]:
    """Appel générique. `messages` peut contenir du texte et des images."""
    payload, latency_ms = mistral_post(
        api_key,
        {
            "model": model or MISTRAL_MODEL,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            "max_tokens": max_tokens,
        },
    )
    choice = payload["choices"][0]
    content = choice["message"]["content"]
    if choice.get("finish_reason") == "length":
        LOG.warning("réponse coupée au plafond de tokens — récupération partielle")
    return loads_lenient(content), payload.get("usage", {}), latency_ms


# Mermaid vit dans le navigateur : on ne peut pas l'exécuter ici. On attrape donc
# les fautes dont on a la preuve qu'elles cassent le rendu, sans rien deviner.
# Les deux schémas cassés observés étaient des classDiagram : « +Data bus:
# Transfers data » et « +increasingSequence(A_n) A_n ⊆ A_{n+1} ». Le deux-points
# et l'accolade y sont de la grammaire, pas du texte, et aucun guillemet ne peut
# les protéger.
MERMAID_ALLOWED = ("flowchart", "graph", "sequenceDiagram")


def mermaid_problems(code: str) -> list[str]:
    """Fautes certaines dans un diagramme. Vide = rien de détectable ici."""
    lines = [line for line in (code or "").strip().split("\n") if line.strip()]
    if not lines:
        return ["diagramme vide"]
    kind = lines[0].strip().split()[0] if lines[0].strip().split() else ""
    problems = []
    if kind not in MERMAID_ALLOWED:
        problems.append(
            f"type « {kind} » interdit : réécris le même contenu en flowchart, "
            "un nœud par notion, libellés entre guillemets"
        )
    for n, line in enumerate(lines, 1):
        if line.count('"') % 2:
            problems.append(f"ligne {n} : guillemet non fermé")
        if line.count("[") != line.count("]"):
            problems.append(f"ligne {n} : crochets non appariés")
    return problems


def mistral_vision_text(api_key: str, data_url: str) -> tuple[str, dict[str, int], float]:
    """Photo du tableau ou de diapositive → markdown. Sortie libre, pas de JSON."""
    payload, latency_ms = mistral_post(
        api_key,
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
        },
    )
    # Le modèle enveloppe volontiers sa réponse dans ```markdown … ```. La
    # clôture reste ensuite au milieu des sources citées par les notes.
    text = payload["choices"][0]["message"]["content"].strip()
    fence = re.match(r"^```[a-zA-Z]*\n(.*?)\n?```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    return (text, payload.get("usage", {}), latency_ms)


# Au-delà, un seul appel ne tient plus : le JSON est coupé au milieu et tout
# l'appel est perdu. Un cours d'une heure fait couramment 600 segments.
#
# La taille ne vient pas d'une limite technique — le modèle n'utilise que 870
# de ses 8000 tokens de sortie sur une fenêtre de 130 — mais de son attention :
# plus la fenêtre est large, plus il résume au lieu de couvrir. Mesuré sur un
# vrai cours de 46 min (306 segments de parole) :
#   130 segments → 56 blocs, 28 % de la parole couverte, 0,0030 €
#    70 segments → 73 blocs, 32 %, 0,0036 €
#    45 segments → 88 blocs, 34 %, 0,0051 €
# 70 prend l'essentiel du gain en deux fois moins d'appels que 45 — ce qui
# compte, car chaque appel supplémentaire rapproche de la limite par minute.
WINDOW_SEGMENTS = 70
WINDOW_OVERLAP = 4
# Signes de documents joints à chaque fenêtre, toutes pièces confondues.
WINDOW_DOC_BUDGET = 9000

WINDOW_PROMPT = """Tu produis les notes d'une PARTIE d'un cours — pas du cours entier.

{LANGUAGE_RULE}

Mêmes règles que d'habitude : chaque bloc cite ses sources — `sourceSegmentIds` pour
la transcription [s12], `sourceAttachmentIds` pour les DOCUMENTS [a0] fournis plus bas
(photos du tableau, diapositives, polycopiés). Ces documents valent pour tout le cours,
pas seulement pour cette partie : sers-t'en dès qu'ils éclairent ce qui est dit ici, et
cite les deux quand une photo confirme l'oral. Rien d'inventé, pas de section creuse,
pas de bloc tiré d'une phrase administrative.
Les formules dictées deviennent des blocs "formula" en LaTeX ; les symboles dans le
texte s'écrivent entre $...$.

COUVERTURE — la faute la plus coûteuse est l'oubli, pas la longueur.
Ces notes REMPLACENT le cours : l'étudiant n'a pas l'enregistrement sous la main, il
n'a que toi. Chaque idée que l'enseignant développe — un mécanisme expliqué, un
exemple détaillé, une distinction posée, un chiffre donné, une consigne d'examen —
doit se retrouver dans un bloc. Tu n'as pas à choisir les trois plus importantes :
prends-les toutes. Un passage de cours développé qui ne laisse aucune trace est une
erreur au même titre qu'une invention.

STRUCTURE — c'est ce qui distingue des notes d'une transcription reformatée.
Ouvre par un titre de section (heading, level 2) qui nomme ce dont il est question,
et découpe la partie en autant de sections qu'il y a de sujets réellement traités.
Dès qu'une idée se décline, utilise des puces plutôt qu'un paragraphe : trois points
en liste se relisent, un paragraphe de huit lignes ne se relit pas. Un terme technique
introduit devient une "definition". Un enchaînement de paragraphes sans titre ni liste
est un échec.

Cette exigence de couverture ne contredit pas l'interdiction des sections creuses :
on ne crée pas de section pour un titre annoncé sans suite, mais tout ce qui EST
développé doit apparaître.

Tu ne produis NI titre général, NI résumé, NI glossaire — quelqu'un d'autre s'en charge
sur l'ensemble. Uniquement les blocs de cette partie, dans l'ordre où les choses sont dites.

Réponds par : {"blocks":[ ... ]}
Chaque bloc utilise EXACTEMENT les champs `type` et `text` — pas `kind`, pas `content`.
Types disponibles : heading (level 2 ou 3), paragraph, bullets (items), definition
(term + definition), callout (kind: a-retenir|exemple|attention|question-ouverte),
formula (latex + caption)."""

SUMMARY_PROMPT = """On te donne le plan et les points d'un cours déjà découpé en blocs.

{LANGUAGE_RULE}

Produis uniquement l'en-tête du document et son glossaire :
{"title":"titre du cours, court et précis","summary":"deux ou trois phrases sur ce que
la séance a couvert","glossary":[{"term":"...","definition":"...","sourceSegmentIds":["s12"]}]}

Le glossaire reprend les termes techniques réellement définis dans le cours, avec
l'identifiant du segment où ils apparaissent. Huit entrées au maximum."""


BLOCK_TYPES = {"heading", "paragraph", "bullets", "definition", "callout", "formula"}


CALLOUT_KINDS = {"a-retenir", "exemple", "attention", "question-ouverte"}


def coerce_block(raw: Any) -> dict[str, Any] | None:
    """
    Ramène un bloc à la forme canonique, ou renvoie None s'il est inutilisable.

    Le modèle produit au moins trois formes selon les appels :
      {"type":"paragraph","text":"..."}          la forme demandée
      {"paragraph":"..."}                        la clé porte le type
      {"kind":"paragraph","content":"..."}       autres noms de champs

    Courir après chaque variante par le prompt ne marche pas — un petit modèle
    dérive sur la forme bien avant de dériver sur le fond. On accepte donc les
    synonymes ici. Le coût de la rigidité était concret : la moitié des blocs
    d'un cours de 46 minutes jetés, puis un plan vide, puis un titre inventé.
    """
    if not isinstance(raw, dict):
        return None
    block = dict(raw)

    kind = None
    if block.get("type") in BLOCK_TYPES:
        kind = block["type"]
    elif block.get("kind") in BLOCK_TYPES:
        kind = block.pop("kind")
    elif block.get("blockType") in BLOCK_TYPES:
        kind = block.pop("blockType")
    elif block.get("kind") in CALLOUT_KINDS:
        kind = "callout"                      # un encadré qui n'a annoncé que sa nature
    else:
        for candidate in BLOCK_TYPES:
            if candidate not in block:
                continue
            value = block.pop(candidate)
            kind = candidate
            if candidate == "bullets" and isinstance(value, list):
                block["items"] = value
            elif candidate == "definition" and isinstance(value, dict):
                block.update(value)
            elif candidate == "formula" and isinstance(value, str):
                block.setdefault("latex", value)
            elif isinstance(value, str):
                block.setdefault("text", value)
            break
    if kind is None:
        return None
    block["type"] = kind

    for alias in ("content", "body", "value", "paragraph"):
        if not str(block.get("text", "")).strip() and isinstance(block.get(alias), str):
            block["text"] = block[alias]
    for alias in ("sources", "segmentIds", "sourceSegments", "source"):
        if not block.get("sourceSegmentIds") and isinstance(block.get(alias), list):
            block["sourceSegmentIds"] = block[alias]

    if kind == "heading":
        block.setdefault("level", 2)
    elif kind == "bullets":
        if not isinstance(block.get("items"), list):
            raw_items = block.get("text") or ""
            block["items"] = [raw_items] if raw_items else []
        block["items"] = [str(i).strip() for i in block["items"] if str(i).strip()]
        if not block["items"]:
            return None
    elif kind == "callout":
        if block.get("kind") not in CALLOUT_KINDS:
            block["kind"] = "a-retenir"
    elif kind == "definition":
        block.setdefault("term", "")
        if not str(block.get("definition", "")).strip():
            block["definition"] = block.get("text", "")
        if not str(block["definition"]).strip():
            return None
    elif kind == "formula":
        for alias in ("latex", "tex", "formula", "text"):
            if str(block.get(alias, "")).strip():
                block["latex"] = block[alias]
                break
        if not str(block.get("latex", "")).strip():
            return None

    if kind in ("heading", "paragraph", "callout") and not str(block.get("text", "")).strip():
        return None
    return block


def locate_in_attachments(
    block: dict[str, Any], attachments: list[dict[str, Any]], top: int = 2
) -> list[str]:
    """
    Même recherche que dans la transcription, mais du côté des documents.

    Un bloc tiré d'une photo du tableau n'a aucun recouvrement avec la parole du
    moment : sans cette piste il partait sans source et se faisait écarter, ce
    qui revenait à jeter ce que la photo apportait.
    """
    produced = content_words(block_text_of(block))
    if not produced:
        return []
    scored = []
    for i, attachment in enumerate(attachments):
        if not isinstance(attachment, dict):
            continue
        overlap = len(produced & content_words(str(attachment.get("text") or "")))
        # Deux mots pleins partagés suffisent à proposer la piste : c'est
        # verify_anchor qui tranche ensuite. Un seul mot serait du hasard.
        if overlap >= 2:
            scored.append((overlap, i))
    scored.sort(reverse=True)
    return [f"a{i}" for _, i in scored[:top]]


def locate_in_window(
    block: dict[str, Any], segments: list[dict[str, Any]], lo: int, hi: int, top: int = 3
) -> list[str]:
    """
    Retrouve d'où vient un bloc quand le modèle n'a pas cité ses sources.

    En génération fenêtrée le modèle omet presque toujours `sourceSegmentIds`.
    Le supplier dans le prompt ne marche pas ; on cherche donc nous-mêmes, par
    recouvrement lexical, les segments de la fenêtre les plus proches du bloc.

    Ce n'est pas un pis-aller : le résultat est souvent plus juste qu'une
    citation du modèle, et surtout il reste vérifiable — un bloc sans aucun
    recouvrement ne reçoit aucune source et sera écarté plus loin.
    """
    produced = content_words(block_text_of(block))
    if not produced:
        return []
    scored = []
    for i in range(lo, hi):
        overlap = len(produced & content_words(segments[i].get("text", "")))
        if overlap:
            scored.append((overlap, i))
    if not scored:
        return []
    scored.sort(reverse=True)
    best = [i for _, i in scored[:top]]
    return [f"s{i}" for i in sorted(best)]


def speech_indices(segments: list[dict[str, Any]]) -> list[int]:
    """
    Indices GLOBAUX des segments qui portent réellement de la parole.

    Deux sortes de déchet, qui demandent deux critères différents.

    Le bruit de fond décodé comme de la parole s'attrape à la confiance. Mais
    Whisper produit aussi, sur les silences, des ritournelles qu'il affirme avec
    aplomb : sur un vrai cours, « Thank you. » revient 39 fois, une occurrence à
    0,89 de confiance. Aucun seuil ne l'écarte — c'est la répétition qui le
    trahit, pas l'incertitude.

    Mesuré sur un cours d'informatique d'une heure : 154 segments sur 561 sont
    des artefacts, soit 27 % des segments pour 6 % des mots. Exactement le
    profil du vide.
    """
    counts: dict[str, int] = {}
    normalised: list[str] = []
    for seg in segments:
        text = re.sub(r"[^a-z0-9 ]", "", str(seg.get("text") or "").lower()).strip()
        normalised.append(text)
        counts[text] = counts.get(text, 0) + 1

    kept = []
    for i, seg in enumerate(segments):
        text = normalised[i]
        words = text.split()
        if not words:
            continue
        # Trois mots ou moins, répétés quatre fois ou plus dans la séance :
        # c'est une ritournelle de silence, pas un enseignant qui insiste.
        if len(words) <= 3 and counts[text] >= 4:
            continue
        # Les anciens documents et certaines contributions importées n'ont pas
        # de score de confiance. « Inconnu » n'est pas « inaudible » : les jeter
        # ferait disparaître tout leur audio des notes partagées. On n'applique
        # le seuil que lorsque Whisper a effectivement fourni un score.
        confidence = seg.get("avgConfidence")
        if confidence is not None and float(confidence) < MIN_SEGMENT_CONFIDENCE:
            continue
        kept.append(i)
    return kept


def generate_windowed(
    api_key: str, segments: list[dict[str, Any]], attachments: list[dict[str, Any]],
    language_rule: str, indices: list[int] | None = None,
) -> tuple[dict[str, Any], dict[str, int], float]:
    """
    Génère les notes d'un long cours en plusieurs passes.

    Un cours d'une heure produit plus de notes qu'un appel ne peut en écrire.
    On découpe donc la transcription en fenêtres, on génère les blocs de chacune,
    puis un dernier appel rédige le titre, le résumé et le glossaire sur
    l'ensemble. Les identifiants de segments restent GLOBAUX pour que l'ancrage
    et les horodatages continuent de pointer au bon endroit.
    """
    started = time.perf_counter()
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    blocks: list[dict[str, Any]] = []
    failures: list[Exception] = []

    # Les documents accompagnent CHAQUE fenêtre, pas seulement la première.
    # Une photo du tableau prise à la cinquantième minute doit être visible de
    # la fenêtre qui couvre la cinquantième minute — sinon elle ne sert à rien,
    # et le bloc qui la cite est rejeté faute d'ancre vérifiable.
    context = ""
    if attachments:
        # Un PDF de polycopié fait plusieurs dizaines de milliers de signes :
        # répété à l'identique dans six fenêtres, il coûterait plus cher que le
        # cours lui-même. On partage donc un budget entre les documents.
        budget = max(1200, WINDOW_DOC_BUDGET // max(1, len(attachments)))
        context = "\n\nDOCUMENTS FOURNIS — photos du tableau, diapositives, polycopiés\n" + "\n\n".join(
            f"[a{i}] {a['name']}\n{a['text'][:budget]}" for i, a in enumerate(attachments)
        )

    # Le filtre de bruit ne s'appliquait qu'aux cours courts : les fenêtres se
    # construisaient sur les segments bruts. Un cours d'une heure — le seul cas
    # qui passe par ici — recevait donc le bruit en entier, et les fenêtres se
    # remplissaient de « Thank you. » au lieu du cours.
    usable = speech_indices(segments) if indices is None else indices
    starts = range(0, len(usable), WINDOW_SEGMENTS - WINDOW_OVERLAP)
    windows = [(i, min(i + WINDOW_SEGMENTS, len(usable))) for i in starts]
    windows = [w for w in windows if w[1] > w[0]]
    LOG.info("cours long : %d segments dont %d de parole → %d fenêtres",
             len(segments), len(usable), len(windows))

    for n, (lo, hi) in enumerate(windows, 1):
        # lo/hi indexent la liste de parole ; les identifiants restent globaux.
        body = "\n".join(f"[s{usable[k]}] {segments[usable[k]]['text']}" for k in range(lo, hi))
        header = f"PARTIE {n} SUR {len(windows)} DU COURS\n"
        try:
            doc, u, _ = mistral_chat(
                api_key,
                [
                    {"role": "system", "content": WINDOW_PROMPT.replace("{LANGUAGE_RULE}", language_rule)},
                    {"role": "user", "content": header + body + context},
                ],
                max_tokens=8000,
            )
        except Exception as exc:  # noqa: BLE001 — une fenêtre ratée ne doit pas perdre les autres
            LOG.exception("fenêtre %d/%d", n, len(windows))
            failures.append(exc)
            continue
        for key in usage:
            usage[key] += u.get(key, 0)
        got = doc.get("blocks") if isinstance(doc, dict) else doc
        if isinstance(got, list):
            for candidate in got:
                fixed = coerce_block(candidate)
                if fixed is None:
                    continue
                if not fixed.get("sourceSegmentIds"):
                    found = locate_in_window(fixed, segments, usable[lo], usable[hi - 1] + 1)
                    if found:
                        fixed["sourceSegmentIds"] = found
                # Ni segment ni document cité : le bloc vient peut-être d'une
                # photo. On cherche avant de l'abandonner.
                if not fixed.get("sourceSegmentIds") and not fixed.get("sourceAttachmentIds"):
                    found = locate_in_attachments(fixed, attachments)
                    if found:
                        fixed["sourceAttachmentIds"] = found
                blocks.append(fixed)

    # En-tête et glossaire à partir des blocs, pas de la transcription entière.
    outline = "\n".join(
        line for line in (
            f"- {b.get('text') or b.get('term') or b.get('caption') or ' '.join(b.get('items') or [])}"[:180]
            for b in blocks
        ) if line.strip(" -")
    )[:14000]
    head: dict[str, Any] = {}
    # Toutes les fenêtres tombées : c'est une panne, pas un cours sans contenu.
    # Rendre un document vide sans rien dire laissait l'étudiant devant une page
    # blanche en croyant que ses notes étaient impossibles à écrire.
    if failures and len(failures) == len(windows):
        raise failures[-1]
    if not outline.strip():
        # Sans plan, l'appel de synthèse invente un sujet. Mieux vaut pas de titre.
        LOG.warning("aucun bloc exploitable : pas d'en-tête généré")
        return ({"title": "Séance", "summary": "", "blocks": [], "glossary": []},
                usage, (time.perf_counter() - started) * 1000)
    try:
        head, u, _ = mistral_chat(
            api_key,
            [
                {"role": "system", "content": SUMMARY_PROMPT.replace("{LANGUAGE_RULE}", language_rule)},
                {"role": "user", "content": outline},
            ],
            max_tokens=2500,
        )
        for key in usage:
            usage[key] += u.get(key, 0)
    except Exception:  # noqa: BLE001
        LOG.exception("en-tête du document")

    return (
        {
            "title": head.get("title") or "Séance",
            "summary": head.get("summary") or "",
            "blocks": blocks,
            "glossary": head.get("glossary") or [],
        },
        usage,
        (time.perf_counter() - started) * 1000,
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
    payload, latency_ms = mistral_post(
        api_key,
        {
            "model": MISTRAL_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": transcript},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            "max_tokens": 6000,
        },
        timeout=180,
    )
    content = payload["choices"][0]["message"]["content"]
    usage = payload.get("usage", {})
    return json.loads(content), usage, latency_ms


# En développement, les notes vivent dans le dépôt. En service installé, elles
# vivent hors du code — sinon un rsync de mise à jour les écraserait.
DATA_DIR = Path(os.environ.get("AMPHI_DATA_DIR") or (STUDIO_DIR.parent.parent / "data" / "studio"))


AUDIO_DIR = DATA_DIR / "audio"
VERSIONS_DIR = DATA_DIR / "versions"

# L'audio pèse ~14 Mo par heure. Le garder indéfiniment ferait 22 Go par an à
# raison de 130 h de cours par mois — tenable sur un disque, pas sur la carte SD
# d'une carte ARM. On le purge donc, sans jamais toucher aux notes.
AUDIO_RETENTION_DAYS = int(os.environ.get("AMPHI_AUDIO_RETENTION_DAYS", "60"))
KEEP_VERSIONS = 10


def safe_id(raw: Any) -> str:
    """Un identifiant de document ne doit jamais pouvoir sortir de DATA_DIR."""
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "", str(raw or ""))[:64]
    return cleaned or "sans-titre"


def snapshot(doc_id: str, payload: dict[str, Any]) -> None:
    """Garde les dix derniers états d'une séance."""
    folder = VERSIONS_DIR / doc_id
    folder.mkdir(parents=True, exist_ok=True)
    # Millisecondes : deux sauvegardes dans la même seconde — ce qui arrive dès
    # qu'on corrige un titre puis qu'on enregistre — écrasaient la même archive.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")[:-3]
    (folder / f"{stamp}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    for old in sorted(folder.glob("*.json"), reverse=True)[KEEP_VERSIONS:]:
        old.unlink(missing_ok=True)


sys.path.insert(0, str(STUDIO_DIR))
import sessions as S  # noqa: E402

SCHEDULE_FILE = DATA_DIR / "schedule.json"


def user_schedule_path(user_id: str) -> Path:
    """Private schedule storage; never derive a filename from user input."""
    return DATA_DIR / f"schedule-user-{safe_id(user_id)}.json"


def load_user_schedule(user_id: str) -> dict[str, dict[str, Any]]:
    path = user_schedule_path(user_id)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text("utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (ValueError, OSError):
        return {}

# Le serveur est multi-thread : deux étudiants peuvent contribuer à la même
# séance au même instant. Un verrou par clé sérialise le lire-modifier-écrire de
# CETTE séance, sans bloquer les autres. Le petit verrou protège le dictionnaire
# de verrous lui-même.
_SESSION_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def session_lock(key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _SESSION_LOCKS.get(key)
        if lock is None:
            lock = _SESSION_LOCKS[key] = threading.Lock()
        return lock


def session_path(key: str) -> Path:
    return DATA_DIR / f"{safe_id(key)}.json"


def load_session(key: str) -> dict[str, Any]:
    path = session_path(key)
    if path.exists():
        try:
            return json.loads(path.read_text("utf-8"))
        except (ValueError, OSError):
            LOG.warning("séance illisible, réinitialisée : %s", key)
    return S.blank_session(key)


def store_session(key: str, session: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = session_path(key)
    if path.exists():
        try:
            snapshot(safe_id(key), json.loads(path.read_text("utf-8")))
        except (ValueError, OSError):
            pass
    session["savedAt"] = datetime.now(timezone.utc).isoformat()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    _INDEX_CACHE.pop(str(path), None)


def load_schedule() -> dict[str, dict[str, Any]]:
    if SCHEDULE_FILE.exists():
        try:
            return json.loads(SCHEDULE_FILE.read_text("utf-8"))
        except (ValueError, OSError):
            return {}
    return {}


def health_diagnostics() -> dict[str, Any]:
    """Return operational facts safe for an unauthenticated liveness probe.

    Deliberately excludes environment values, paths containing user names, and
    all session content. Queue state is client-side (Tauri) in this deployment.
    """
    now = time.time()
    try:
        usage = os.statvfs(DATA_DIR if DATA_DIR.exists() else DATA_DIR.parent)
        storage = {
            "ok": usage.f_bavail > 0,
            "freeBytes": usage.f_bavail * usage.f_frsize,
            "totalBytes": usage.f_blocks * usage.f_frsize,
        }
    except OSError:
        storage = {"ok": False, "freeBytes": None, "totalBytes": None}
    json_files = list(DATA_DIR.glob("*.json")) if DATA_DIR.exists() else []
    session_files = [p for p in json_files if p.name != "schedule.json"]
    audio_files = [p for p in AUDIO_DIR.rglob("*") if p.is_file()] if AUDIO_DIR.exists() else []
    backup_root = Path(os.environ.get("AMPHI_BACKUP_DIR") or (Path.home() / "Backups" / "amphi"))
    try:
        backups = sorted((p for p in backup_root.iterdir() if p.is_dir()),
                         key=lambda p: p.stat().st_mtime, reverse=True) if backup_root.exists() else []
    except OSError:
        backups = []
    latest_backup = backups[0] if backups else None
    backup_age = round(max(0.0, now - latest_backup.stat().st_mtime), 1) if latest_backup else None
    backup_ok = bool(latest_backup and (latest_backup / "manifest.json").is_file())
    return {
        "storage": {**storage, "dataFiles": len(json_files), "sessionFiles": len(session_files),
                    "audioFiles": len(audio_files)},
        "queue": {"state": "client-side", "pending": None, "diagnostic": "not observable by server"},
        "collaboration": {"activeLocks": sum(1 for lock in _SESSION_LOCKS.values() if lock.locked()),
                          "knownSessions": len(session_files), "lockRegistry": len(_SESSION_LOCKS)},
        "algorithm": {"consensusVersion": S.CONSENSUS_VERSION},
        "backup": {"latestAgeSeconds": backup_age, "available": bool(latest_backup),
                    "valid": backup_ok, "count": len(backups)},
    }


def purge_old_audio() -> int:
    """Supprime l'audio expiré. Les notes, elles, ne sont jamais touchées."""
    if not AUDIO_DIR.exists() or AUDIO_RETENTION_DAYS <= 0:
        return 0
    cutoff = time.time() - AUDIO_RETENTION_DAYS * 86400
    removed = 0
    for f in AUDIO_DIR.rglob("*"):
        if f.is_file() and f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)
            removed += 1
    for folder in AUDIO_DIR.iterdir() if AUDIO_DIR.exists() else []:
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
    return removed


# En dessous, un segment est du bruit décodé comme de la parole. Il reste dans
# la transcription — l'étudiant peut vouloir le voir — mais n'est pas envoyé au
# modèle : le nourrir de « the data and the data of the data » ne l'aide pas.
MIN_SEGMENT_CONFIDENCE = float(os.environ.get("AMPHI_MIN_CONFIDENCE", "0.35"))


def lexicon_for_course(course: str, limit: int = 60) -> list[str]:
    """
    Vocabulaire du cours, tiré des séances déjà transcrites.

    C'est le biasing ASR du §3.2, alimenté par ce qu'on a déjà : les termes du
    glossaire et les titres des séances précédentes du même cours. Whisper
    reconnaît nettement mieux « gradient boosting » ou « heteroskedasticity »
    quand ils figurent dans son prompt initial.
    """
    course = (course or "").strip()
    if not course or not DATA_DIR.exists():
        return []
    terms: list[str] = []
    seen = set()
    for path in sorted(DATA_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            payload = json.loads(path.read_text("utf-8"))
        except (ValueError, OSError):
            continue
        if (payload.get("course") or "").strip() != course:
            continue
        doc = payload.get("doc") or {}
        candidates = [str(e.get("term", "")) for e in (doc.get("glossary") or []) if isinstance(e, dict)]
        candidates += [str(b.get("term", "")) for b in (doc.get("blocks") or [])
                       if isinstance(b, dict) and b.get("type") == "definition"]
        candidates.append(str(doc.get("title", "")))
        for term in candidates:
            term = term.strip()
            key = term.lower()
            if term and key not in seen and 2 < len(term) < 60:
                seen.add(key)
                terms.append(term)
        if len(terms) >= limit:
            break
    return terms[:limit]


# Chaque séance porte ses photos et sa transcription : le fichier pèse vite
# quelques mégaoctets, et la bibliothèque les relisait tous à chaque ouverture.
# La clé est (mtime, taille) — une séance modifiée est relue, les autres non.
_INDEX_CACHE: dict[str, tuple[float, int, dict[str, Any]]] = {}


def list_sessions(on: str = "", course: str = "") -> list[dict[str, Any]]:
    """
    Séances existantes, pour l'écran « rejoindre ». On lit les fichiers qui ont
    une clé de séance, on résume, et on filtre éventuellement par jour ou cours.
    """
    if not DATA_DIR.exists():
        return []
    out = []
    for path in sorted(DATA_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        if path.name == "schedule.json":
            continue
        try:
            payload = json.loads(path.read_text("utf-8"))
        except (ValueError, OSError):
            continue
        if "sessionKey" not in payload:
            continue  # ancien document mono-utilisateur, pas une séance partagée
        summary = S.session_summary(payload)
        if on and not str(summary.get("start") or "").startswith(on):
            continue
        if course and S.slugify(course) not in S.slugify(summary.get("course") or ""):
            continue
        out.append(summary)
    return out


def schedule_now_from_book(book: dict[str, dict[str, Any]], at_iso: str = "") -> list[dict[str, Any]]:
    """Cours d'un carnet en cours ou imminents autour de l'instant donné."""
    if not book:
        return []
    now = datetime.now(timezone.utc)
    if at_iso:
        try:
            now = datetime.fromisoformat(at_iso.replace("Z", "+00:00"))
        except ValueError:
            pass
    hits = []
    for ev in book.values():
        try:
            start = datetime.fromisoformat(str(ev["start"]).replace("Z", "+00:00"))
        except (ValueError, KeyError, TypeError):
            continue
        end = None
        if ev.get("end"):
            try:
                end = datetime.fromisoformat(str(ev["end"]).replace("Z", "+00:00"))
            except ValueError:
                end = None
        end = end or (start + timedelta(hours=2))
        # « en ce moment » avec une marge : on veut proposer le cours quinze
        # minutes avant qu'il commence et un peu après sa fin.
        if start - timedelta(minutes=15) <= now <= end + timedelta(minutes=30):
            hits.append({**ev, "state": "now"})
    hits.sort(key=lambda e: e["start"])
    return hits


def schedule_now(at_iso: str = "") -> list[dict[str, Any]]:
    return schedule_now_from_book(load_schedule(), at_iso)


def list_documents() -> list[dict[str, Any]]:
    """Index de la bibliothèque : un fichier inchangé n'est pas relu."""
    if not DATA_DIR.exists():
        return []
    docs = []
    seen: set[str] = set()
    for path in sorted(DATA_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        if path.name == "schedule.json":
            continue
        key = str(path)
        seen.add(key)
        try:
            info = path.stat()
        except OSError:
            continue
        cached = _INDEX_CACHE.get(key)
        if cached and cached[0] == info.st_mtime and cached[1] == info.st_size:
            docs.append(cached[2])
            continue
        try:
            payload = json.loads(path.read_text("utf-8"))
        except (ValueError, OSError):
            continue
        doc = payload.get("doc") or {}
        if payload.get("sessionKey"):
            # Séance partagée : le titre vient de l'agenda, la durée du plus long
            # enregistrement, et on expose qui a contribué.
            sched = payload.get("schedule") or {}
            recs = payload.get("recordings") or []
            entry = {
                "id": path.stem,
                "sessionKey": payload["sessionKey"],
                "title": doc.get("title") or sched.get("course") or sched.get("title") or path.stem,
                "savedAt": payload.get("savedAt"),
                "blocks": len(doc.get("blocks") or []),
                "diagrams": 0,
                "attachments": len(payload.get("attachments") or []),
                "durationMs": max((r.get("endMs") or 0) for r in recs) if recs else 0,
                "course": (sched.get("course") or "").strip(),
                "chapter": "",
                "contributors": payload.get("contributors") or [],
                "recordingCount": len(recs),
            }
        else:
            entry = {
                "id": path.stem,
                "title": doc.get("title") or payload.get("title") or path.stem,
                "savedAt": payload.get("savedAt"),
                "blocks": len(doc.get("blocks") or []),
                "diagrams": len(payload.get("diagrams") or []),
                "attachments": len(payload.get("attachments") or []),
                "durationMs": (payload.get("segments") or [{}])[-1].get("endMs", 0) if payload.get("segments") else 0,
                "course": (payload.get("course") or "").strip(),
                "chapter": (payload.get("chapter") or "").strip(),
            }
        _INDEX_CACHE[key] = (info.st_mtime, info.st_size, entry)
        docs.append(entry)
    # Une séance supprimée ne doit pas rester en mémoire jusqu'au redémarrage.
    for stale in set(_INDEX_CACHE) - seen:
        _INDEX_CACHE.pop(stale, None)
    return docs


# Un scan de 200 pages passé à la vision coûterait des euros et des minutes
# sans prévenir. Au-delà, on refuse et on dit pourquoi.
PDF_VISION_PAGES = 12
# En dessous, la page n'a pas livré de texte : c'est une image, un schéma, ou
# un scan. Un en-tête et un numéro de page font déjà une quarantaine de signes.
PDF_MIN_CHARS = 60


def decode_data_url(raw: Any) -> bytes:
    """Octets d'un fichier arrivé en data: URL."""
    text = str(raw or "")
    if "," not in text:
        raise ValueError("fichier illisible : data URL attendue")
    try:
        return base64.b64decode(text.split(",", 1)[1])
    except (binascii.Error, ValueError) as exc:
        raise ValueError("fichier illisible : base64 invalide") from exc


def format_cost(millicents: float) -> str:
    """Coût affiché à l'étudiant, dans la convention française."""
    return f"{millicents / 1000:.4f} €".replace(".", ",")


def pdf_page_image(page: Any) -> str | None:
    """Plus grande image intégrée d'une page — celle du scan, pas le logo."""
    try:
        images = list(page.images)
    except Exception:  # noqa: BLE001 — un PDF exotique ne doit pas tout arrêter
        return None
    best = None
    for image in images:
        try:
            blob = image.data
        except Exception:  # noqa: BLE001
            continue
        if blob and (best is None or len(blob) > len(best[1])):
            best = (image.name or "", blob)
    if best is None or len(best[1]) < 8000:
        return None
    kind = "png" if best[1][:4] == b"\x89PNG" else "jpeg"
    return f"data:image/{kind};base64," + base64.b64encode(best[1]).decode()


def text_from_pdf(blob: bytes, name: str, api_key: str = "") -> tuple[str, float]:
    """
    Texte d'un PDF, page par page. Retourne (texte, coût en millicents).

    Deux sortes de PDF arrivent d'un cours : celui exporté depuis LaTeX ou
    PowerPoint, dont le texte se lit directement et gratuitement ; et le scan,
    qui n'est qu'une suite d'images. Le second passe par la lecture visuelle,
    la même que pour une photo de tableau — sinon un polycopié scanné donnerait
    un document vide sans que personne comprenne pourquoi.
    """
    if not PDF_AVAILABLE:
        raise ValueError("la lecture des PDF demande pypdf : python -m pip install pypdf")
    try:
        reader = PdfReader(io.BytesIO(blob))
        if reader.is_encrypted:
            # Un PDF « protégé » sans mot de passe reste courant ; on tente.
            reader.decrypt("")
        pages = list(reader.pages)
    except Exception as exc:  # noqa: BLE001 — pypdf lève une famille d'erreurs
        raise ValueError(f"{name} : PDF illisible ({type(exc).__name__})") from exc
    if not pages:
        raise ValueError(f"{name} ne contient aucune page")

    chunks: list[tuple[int, str]] = []
    scanned: list[int] = []
    for number, page in enumerate(pages, start=1):
        try:
            text = (page.extract_text() or "").strip()
        except Exception:  # noqa: BLE001
            text = ""
        if len(text) >= PDF_MIN_CHARS:
            chunks.append((number, text))
        else:
            scanned.append(number)

    cost = 0.0
    if scanned and not chunks:
        # Document entièrement scanné : c'est le cas qui justifie la lecture
        # visuelle. S'il ne reste qu'une page vide au milieu d'un texte, on
        # l'ignore — ce n'est pas la peine de payer pour un intercalaire.
        if api_key == "":
            raise ValueError(
                f"{name} est un PDF scanné (aucun texte) et MISTRAL_API_KEY est absente."
            )
        if len(scanned) > PDF_VISION_PAGES:
            raise ValueError(
                f"{name} est un scan de {len(scanned)} pages : au-delà de "
                f"{PDF_VISION_PAGES}, découpe-le ou photographie les pages utiles."
            )
        for number in scanned:
            data_url = pdf_page_image(pages[number - 1])
            if data_url is None:
                continue
            text, usage, _ = mistral_vision_text(api_key, data_url)
            cost += (usage.get("prompt_tokens", 0) * IN_MILLICENTS
                     + usage.get("completion_tokens", 0) * OUT_MILLICENTS)
            if text.strip():
                chunks.append((number, text.strip()))
        chunks.sort()

    if not chunks:
        raise ValueError(f"{name} : aucun texte extractible (scan sans image lisible ?)")
    return "\n\n".join(f"— Page {n} —\n{t}" for n, t in chunks), cost


def text_from_office(blob: bytes, name: str) -> str:
    """
    Texte d'un .pptx ou d'un .docx, avec la seule bibliothèque standard.

    Ces formats sont des archives zip de XML. On n'a pas besoin d'un lecteur
    complet : les notes ne veulent que les mots, et le diaporama du prof est le
    document que l'étudiant a le plus souvent sous la main.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as exc:
        raise ValueError(f"{name} n'est pas un fichier Office lisible") from exc

    def runs(xml: str) -> list[str]:
        # <a:t> pour PowerPoint, <w:t> pour Word — mêmes balises de texte brut.
        found = re.findall(r"<(?:a|w):t[^>]*>(.*?)</(?:a|w):t>", xml, re.DOTALL)
        out = []
        for piece in found:
            clean = re.sub(r"<[^>]+>", "", piece)
            clean = html.unescape(clean).strip()
            if clean:
                out.append(clean)
        return out

    slides = [n for n in archive.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)]
    if slides:
        # Diapo 2 vient après la diapo 10 dans l'ordre alphabétique : on trie
        # sur le numéro, sinon le plan du cours ressort mélangé.
        numbered = sorted(
            ((int(re.search(r"(\d+)", n.rsplit("/", 1)[-1]).group(1)), n) for n in slides),
        )
        blocks = []
        # On garde le numéro du fichier, pas un compteur : c'est celui que
        # l'étudiant verra en rouvrant le diaporama du prof, même si une
        # diapositive a été supprimée entre-temps.
        for number, entry in numbered:
            lines = runs(archive.read(entry).decode("utf-8", "replace"))
            if lines:
                blocks.append(f"— Diapositive {number} —\n" + "\n".join(lines))
        if not blocks:
            raise ValueError(f"{name} ne contient aucun texte (diapositives en images ?)")
        return "\n\n".join(blocks)

    if "word/document.xml" in archive.namelist():
        lines = runs(archive.read("word/document.xml").decode("utf-8", "replace"))
        if not lines:
            raise ValueError(f"{name} ne contient aucun texte")
        return "\n".join(lines)

    raise ValueError(f"{name} : format Office non reconnu (attendu .pptx ou .docx)")


class StudioHandler(AsrHandler):
    """Étend le serveur ASR : sert l'interface, les pièces jointes, les notes et les schémas."""

    def _read_json(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length)) if length else {}

    def _api_key(self) -> str:
        return os.environ.get("MISTRAL_API_KEY", "")

    def log_request(self, code: str | int = "-", size: str | int = "-") -> None:
        """
        Journal d'accès lisible, hors ressources statiques.

        Sur un serveur partagé, savoir ce qui se passe compte plus que d'avoir
        un journal court — mais lister chaque police et chaque image le rendrait
        illisible.
        """
        if any(self.path.startswith(p) for p in ("/vendor/", "/bench-audio/", "/audio/")):
            return
        origin = self.headers.get("Origin")
        source = " (app)" if origin and origin.startswith("tauri") else ""
        LOG.info("%s %s → %s%s", self.command, self.path.split("?")[0], code, source)

    def _cors(self) -> None:
        """
        L'application de bureau appelle depuis l'origine `tauri://localhost`.
        Sans ces en-têtes, le navigateur embarqué refuse toutes les requêtes.
        On renvoie l'origine exacte plutôt que « * » : un joker interdit
        l'envoi des identifiants.
        """
        origin = self.headers.get("Origin")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.send_header("Vary", "Origin")

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(blob)))
        self._cors()
        self.end_headers()
        self.wfile.write(blob)

    def do_OPTIONS(self) -> None:  # noqa: N802
        """Préflight : le navigateur le déclenche dès qu'on envoie un en-tête maison."""
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, Authorization, X-Mime-Type, X-Lexicon, "
                         "X-Previous-Text, X-Language")
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _identity(self) -> dict | None:
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            return identity_auth.authenticate(header[7:].strip())
        return None

    def _authorized(self) -> bool:
        """Bearer identity first, then temporary shared Basic migration auth."""
        if self._identity():
            return True
        if not AMPHI_PASSWORD:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8", "replace")
        except (ValueError, binascii.Error):
            return False
        _, _, given = decoded.partition(":")
        return hmac.compare_digest(given, AMPHI_PASSWORD)

    def _demand_auth(self, status: int = 401) -> None:
        body = b'{"error":"authentification requise"}' if status == 401 else b'{"error":"droits insuffisants"}'
        if status == 403:
            self.send_response(403)
        else:
            self.send_response(401)
        self.send_header("WWW-Authenticate", f'Basic realm="{REALM}", charset="UTF-8"')
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _resource_allowed(self, owner: str | None, *, claim: bool = False) -> bool:
        """Bearer identities may only access resources they own; legacy auth stays shared."""
        actor = self._identity()
        if not actor:
            return True
        user_id = str(actor.get("id") or "")
        if owner and not hmac.compare_digest(str(owner), user_id):
            return False
        return True

    def _resource_owner(self, payload: dict[str, Any]) -> str | None:
        return payload.get("_ownerId") if isinstance(payload, dict) else None

    def _deny_resource(self) -> None:
        self._send(403, {"error": "accès refusé"})

    # ------------------------------------------------------------------ GET

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in ("/compatibility", "/api/compatibility"):
            raw = (parse_qs(parsed.query).get("clientProtocol") or [""])[0]
            try:
                client_protocol = int(raw) if raw else None
            except ValueError:
                client_protocol = None
            compatible = (client_protocol is None or
                          client_protocol >= MIN_CLIENT_COMPATIBILITY_VERSION)
            self._send(200, {
                "compatible": compatible,
                "serverVersion": SERVER_VERSION,
                "protocolVersion": COMPATIBILITY_VERSION,
                "minClientProtocolVersion": MIN_CLIENT_COMPATIBILITY_VERSION,
                "clientProtocolVersion": client_protocol,
            })
            return
        if parsed.path == "/health":
            payload = {"ok": True, "service": "amphi-studio",
                       "serverVersion": SERVER_VERSION,
                       "protocolVersion": COMPATIBILITY_VERSION,
                       **health_diagnostics()}
            self._send(200, payload)
            return
        if urlparse(self.path).path == "/auth/admin/users":
            actor = self._identity()
            if not actor or actor.get("role") != "admin":
                self._send(403, {"error": "droits administrateur requis"})
            else:
                self._send(200, {"users": identity_auth.list_users()})
            return
        if urlparse(self.path).path == "/auth/me":
            user = self._identity()
            self._send(200, {"user": user}) if user else self._send(401, {"error": "authentification requise"})
            return
        # /health reste ouvert : c'est ce que la sonde du tunnel interroge.
        if self.path != "/health" and not self._authorized():
            self._demand_auth()
            return
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
            self._send_file(target, types.get(target.suffix, "application/octet-stream"),
                            cache="public, max-age=604800")
            return
        if self.path.startswith("/audio/"):
            parts = self.path[len("/audio/"):].split("/")
            if len(parts) != 2:
                self._send(404, {"error": "chemin audio invalide"})
                return
            doc_id = safe_id(parts[0])
            session = load_session(doc_id)
            if not self._resource_allowed(self._resource_owner(session)):
                self._deny_resource(); return
            target = AUDIO_DIR / doc_id / re.sub(r"[^0-9a-zA-Z.]", "", parts[1])
            self._send_file(target, "audio/webm" if target.suffix == ".webm" else "audio/mp4",
                            cache="private, max-age=86400")
            return
        if self.path.startswith("/versions"):

            doc_id = safe_id((parse_qs(urlparse(self.path).query).get("id") or [""])[0])
            current = load_session(doc_id)
            if not self._resource_allowed(self._resource_owner(current)):
                self._deny_resource(); return
            folder = VERSIONS_DIR / doc_id
            out = []
            if folder.exists():
                for f in sorted(folder.glob("*.json"), reverse=True):
                    try:
                        payload = json.loads(f.read_text("utf-8"))
                    except (ValueError, OSError):
                        continue
                    doc = payload.get("doc") or {}
                    out.append({"stamp": f.stem, "savedAt": payload.get("savedAt"),
                                "title": doc.get("title"), "blocks": len(doc.get("blocks") or [])})
            self._send(200, {"versions": out})
            return
        # Banc de mesure : décide si la transcription peut passer côté navigateur.
        if self.path in ("/bench", "/bench/"):
            self._send_file(STUDIO_DIR.parent.parent / "bench" / "webgpu" / "index.html",
                            "text/html; charset=utf-8")
            return
        if self.path.startswith("/bench-audio/"):
            name = re.sub(r"[^0-9a-zA-Z._-]", "", self.path[len("/bench-audio/"):])
            self._send_file(STUDIO_DIR.parent.parent / "bench" / "fixtures" / name,
                            "audio/wav", cache="private, max-age=3600")
            return
        if self.path.startswith("/lexicon"):

            course = (parse_qs(urlparse(self.path).query).get("course") or [""])[0]
            self._send(200, {"course": course, "terms": lexicon_for_course(course)})
            return
        if self.path.startswith("/search"):
            q = (parse_qs(urlparse(self.path).query).get("q") or [""])[0]
            self._send(200, {"query": q, "results": search_documents(q)})
            return
        if self.path == "/docs":
            self._send(200, {"docs": list_documents()})
            return
        if self.path.startswith("/sessions"):
            q = parse_qs(urlparse(self.path).query)
            on = (q.get("on") or [""])[0]          # YYYY-MM-DD, vide = tout
            course = (q.get("course") or [""])[0]
            self._send(200, {"sessions": list_sessions(on=on, course=course)})
            return
        if urlparse(self.path).path == "/schedule/me":
            identity = self._identity()
            if not identity:
                self._demand_auth()
                return
            book = load_user_schedule(str(identity.get("id") or ""))
            self._send(200, {"now": schedule_now_from_book(book), "book": list(book.values())})
            return
        if self.path.startswith("/schedule"):
            q = parse_qs(urlparse(self.path).query)
            at = (q.get("at") or [""])[0]
            self._send(200, {"now": schedule_now(at), "book": list(load_schedule().values())})
            return
        if self.path.startswith("/load"):
            doc_id = self.path.partition("?id=")[2] or "default"
            path = DATA_DIR / f"{re.sub(r'[^a-zA-Z0-9_-]', '', doc_id)}.json"
            loaded = json.loads(path.read_text("utf-8")) if path.exists() else {"empty": True}
            if not self._resource_allowed(self._resource_owner(loaded)):
                self._deny_resource(); return
            self._send(200, loaded)
            return
        super().do_GET()

    def _send_file(self, path: Path, content_type: str, cache: str | None = None) -> None:
        if not path.exists():
            self._send(404, {"error": f"{path.name} introuvable"})
            return
        blob = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(blob)))
        # L'interface change à chaque correctif : sans cet en-tête, le navigateur
        # garde l'ancienne page et on croit que rien n'a été corrigé. Les
        # ressources vendorisées, elles, ne bougent jamais — on les laisse en cache.
        self.send_header("Cache-Control", cache or "no-store, must-revalidate")
        self._cors()
        self.end_headers()
        self.wfile.write(blob)

    # ----------------------------------------------------------------- POST

    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        if route == "/auth/login":
            try:
                body = self._read_json()
                result = identity_auth.login(str(body.get("username", "")), str(body.get("password", "")))
                self._send(200, result) if result else self._send(401, {"error": "identifiants invalides"})
            except Exception as exc:
                self._send(400, {"error": str(exc)})
            return
        if route == "/auth/logout":
            header = self.headers.get("Authorization", "")
            if header.startswith("Bearer "): identity_auth.logout(header[7:].strip())
            self._send(200, {"ok": True}); return
        if route.startswith("/auth/admin/"):
            actor = self._identity()
            if not actor or actor.get("role") != "admin":
                self._send(403, {"error": "droits administrateur requis"}); return
            try:
                body = self._read_json()
                if route == "/auth/admin/invite":
                    result = identity_auth.admin_invite(str(body.get("username", "")), str(body.get("displayName", "")), str(body.get("role", "contributor")))
                    self._send(201, result)
                elif route in {"/auth/admin/disable", "/auth/admin/enable"}:
                    result = identity_auth.admin_set_disabled(str(body.get("userId", "")), route.endswith("disable"))
                    self._send(200, {"user": result})
                elif route == "/auth/admin/rotate":
                    self._send(200, identity_auth.admin_rotate(str(body.get("userId", ""))))
                elif route == "/auth/admin/revoke":
                    self._send(200, {"revoked": identity_auth.admin_revoke(str(body.get("userId", "")))})
                else: self._send(404, {"error": "route introuvable"})
            except (ValueError, KeyError) as exc: self._send(400, {"error": str(exc)})
            return
        if not self._authorized():
            self._demand_auth()
            return
        routes = {
            "/notes": self.handle_notes,
            "/attachment": self.handle_attachment,
            "/diagram": self.handle_diagram,
            "/save": self.handle_save,
            "/delete": self.handle_delete,
            "/move": self.handle_move,
            "/audio": self.handle_audio,
            "/restore": self.handle_restore,
            "/contribute": self.handle_contribute,
            "/session-notes": self.handle_session_notes,
            "/session-doc": self.handle_session_doc,
            "/schedule/import": self.handle_schedule_import,
            "/schedule/import/me": self.handle_user_schedule_import,
        }
        # `self.path` contient la chaîne de requête : /audio?id=… ne matchait
        # aucune route et repartait en 404 sans explication.

        handler = routes.get(urlparse(self.path).path)
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

        if kind == "pdf":
            blob = decode_data_url(payload.get("dataUrl"))
            text, cost = text_from_pdf(blob, name, self._api_key())
            self._send(200, {"kind": "pdf", "name": name, "text": text,
                             "costEuros": format_cost(cost)})
            return

        if kind == "office":
            text = text_from_office(decode_data_url(payload.get("dataUrl")), name)
            self._send(200, {"kind": "office", "name": name, "text": text, "costEuros": "0,0000 €"})
            return

        if kind != "image":
            raise ValueError("kind doit valoir 'image', 'pdf', 'office' ou 'text'")

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
                "costEuros": format_cost(cost),
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
        lang_code, lang_rule = resolve_language(payload.get("language", "auto"), segments, attachments)
        # §6.4 : une génération ne réécrit jamais ce qu'un humain a touché.
        keep = payload.get("keepEdited") or []

        if not segments and not attachments:
            raise ValueError("ni transcription ni pièce jointe : rien à résumer")

        # Les indices restent GLOBAUX : un bloc doit pouvoir citer [s412] même
        # si les segments 400 à 411 ont été écartés comme bruit.
        speech = speech_indices(segments)
        usable = [(i, segments[i]) for i in speech]
        dropped = len(segments) - len(usable)
        if dropped:
            LOG.info("bruit écarté avant génération : %d segments sur %d", dropped, len(segments))

        parts = []
        if usable:
            parts.append("TRANSCRIPTION DE LA SÉANCE\n"
                         + "\n".join(f"[s{i}] {s['text']}" for i, s in usable))
        if attachments and not usable:
            # Sans transcription, la règle « ne rédige rien qui ne soit dit »
            # n'a plus de référent : il faut la redire sur les documents, sinon
            # le modèle se croit sans source et ne produit rien.
            parts.append(
                "PAS DE TRANSCRIPTION : la séance n'a pas été enregistrée. Les DOCUMENTS "
                "ci-dessous sont l'unique source. Tire-en des notes complètes et cite-les "
                "dans sourceAttachmentIds. N'invente rien qui n'y figure pas."
            )
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
                if len(speech) > WINDOW_SEGMENTS:
                    doc, usage, latency_ms = generate_windowed(
                        api_key, segments, attachments, lang_rule, speech)
                else:
                    doc, usage, latency_ms = mistral_chat(
                        api_key,
                        [
                            {"role": "system", "content": SYSTEM_PROMPT.replace("{LANGUAGE_RULE}", lang_rule)},
                            {"role": "user", "content": "\n\n".join(parts)},
                        ],
                        max_tokens=8000,
                    )
                engine = MISTRAL_MODEL
            except urllib.error.HTTPError as exc:
                problems.append(mistral_error_text(exc))
            except ConnectionError as exc:
                problems.append(str(exc))
        else:
            problems.append("MISTRAL_API_KEY absente.")

        if doc is None:
            if not local_llm_available():
                self._send(503, {"error": " ".join(problems) + f" Modèle local ({LOCAL_LLM_MODEL}) pas encore téléchargé."})
                return
            doc, usage, latency_ms = generate_local("\n\n".join(parts), lang_rule)
            engine = LOCAL_LLM_MODEL.split("/")[-1] + " (local)"

        kept, rejected, enrichments = [], 0, []
        for block in doc.get("blocks") or []:
            # Le modèle glisse parfois une chaîne dans la liste de blocs : sans
            # ce garde-fou, tout l'appel échoue sur un AttributeError et on
            # perd des notes déjà générées.
            if not isinstance(block, dict):
                rejected += 1
                continue
            # Un bloc d'enrichissement assume de ne pas venir du cours : il n'a pas
            # d'ancre, et l'interface le présente à part. La garantie de traçabilité
            # devient « tout est soit ancré au cours, soit signalé comme extérieur ».
            if block.get("type") == "enrichment":
                if enrich and (block.get("text") or "").strip():
                    enrichments.append(block)
                continue
            fixed = coerce_block(block)
            if fixed is None:
                rejected += 1
                continue
            block = fixed
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
                if isinstance(extra, dict) and str(extra.get("text") or "").strip():
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
        for entry in doc.get("glossary") or []:
            # Un glossaire mal formé ne doit pas faire perdre les notes.
            if not isinstance(entry, dict) or not str(entry.get("term", "")).strip():
                continue
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
                "noisySegments": dropped,
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

        lang_rule = LANGUAGE_RULES.get(str(payload.get("language") or "en")[:2], LANGUAGE_RULES["en"])
        prefix = f"{lang_rule} Les libellés du schéma doivent être dans cette langue.\n\n"
        user = prefix + (context if instruction == "" else f"Consigne : {instruction}\n\nPassage :\n{context}")
        messages = [{"role": "system", "content": DIAGRAM_PROMPT}, {"role": "user", "content": user}]
        # Le navigateur peut renvoyer le diagramme et l'erreur du vrai parseur :
        # c'est le signal le plus sûr qui soit, on le rend au modèle tel quel.
        broken = str(payload.get("brokenMermaid") or "").strip()
        if broken:
            messages.append({"role": "assistant", "content": json.dumps({"mermaid": broken})})
            messages.append({"role": "user", "content":
                "Ce diagramme ne passe pas le parseur Mermaid : "
                + str(payload.get("parserError") or "erreur inconnue")
                + "\nRéécris-le en flowchart, même contenu, tous les libellés entre guillemets."})

        doc, usage, latency_ms = mistral_chat(api_key, messages, max_tokens=1500)
        cost = usage.get("prompt_tokens", 0) * IN_MILLICENTS + usage.get("completion_tokens", 0) * OUT_MILLICENTS

        # Une seule reprise : si le diagramme porte une faute qu'on sait fatale,
        # on la donne au modèle plutôt que de livrer un schéma qui ne s'affichera pas.
        problems = mermaid_problems(doc.get("mermaid", ""))
        if problems and doc.get("mermaid", "").strip():
            LOG.info("schéma refusé (%s) — nouvelle tentative", "; ".join(problems[:2]))
            retry = messages + [
                {"role": "assistant", "content": json.dumps(doc, ensure_ascii=False)},
                {"role": "user", "content": "Ce diagramme ne peut pas être rendu : "
                 + " ; ".join(problems[:4]) + ". Réécris-le en flowchart."},
            ]
            try:
                fixed, u2, _ = mistral_chat(api_key, retry, max_tokens=1500)
                if not mermaid_problems(fixed.get("mermaid", "")):
                    doc = fixed
                    cost += (u2.get("prompt_tokens", 0) * IN_MILLICENTS
                             + u2.get("completion_tokens", 0) * OUT_MILLICENTS)
            except Exception:  # noqa: BLE001 — on garde le premier résultat
                LOG.exception("reprise du schéma")
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

    def handle_audio(self) -> None:
        """
        Reçoit une partie d'enregistrement et la garde sur disque.

        Sans ça, rouvrir une séance donnait le texte mais plus le son : les
        horodatages restaient affichés en ne renvoyant nulle part, ce qui vidait
        de sens la traçabilité qui est le principe du produit.
        """

        params = parse_qs(urlparse(self.path).query)
        doc_id = safe_id((params.get("id") or [""])[0])
        seq = int((params.get("seq") or ["0"])[0])
        suffix = ".mp4" if "mp4" in (params.get("mime") or [""])[0] else ".webm"

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ValueError("corps audio vide")
        blob = self.rfile.read(length)

        folder = AUDIO_DIR / doc_id
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{seq:04d}{suffix}"
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_bytes(blob)
        temp.replace(path)
        self._send(200, {"ok": True, "url": f"/audio/{doc_id}/{path.name}", "bytes": len(blob)})

    def handle_move(self) -> None:
        """
        Range une séance sans la charger côté navigateur.

        Un aller-retour /load puis /save renverrait le document entier — 567 Ko
        pour un cours d'une heure — juste pour changer deux champs. On modifie
        sur place, en écriture atomique comme la sauvegarde.
        """
        payload = self._read_json()
        path = DATA_DIR / f"{safe_id(payload.get('id'))}.json"
        if not path.exists():
            raise ValueError("séance introuvable")
        doc = json.loads(path.read_text("utf-8"))
        doc["course"] = str(payload.get("course") or "").strip()
        doc["chapter"] = str(payload.get("chapter") or "").strip()
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)
        self._send(200, {"ok": True, "course": doc["course"], "chapter": doc["chapter"]})

    def handle_delete(self) -> None:
        payload = self._read_json()
        doc_id = safe_id(payload.get("id"))
        path = DATA_DIR / f"{doc_id}.json"
        if path.exists():
            current = json.loads(path.read_text("utf-8"))
            if not self._resource_allowed(self._resource_owner(current)):
                self._deny_resource(); return
        path.unlink(missing_ok=True)
        # L'audio et l'historique partent avec la séance : garder des morceaux
        # d'une séance supprimée serait une surprise désagréable côté RGPD.
        import shutil

        shutil.rmtree(AUDIO_DIR / doc_id, ignore_errors=True)
        shutil.rmtree(VERSIONS_DIR / doc_id, ignore_errors=True)
        self._send(200, {"ok": True, "id": doc_id})

    def handle_restore(self) -> None:
        """Remet une version antérieure en place, après avoir archivé l'actuelle."""
        payload = self._read_json()
        doc_id = safe_id(payload.get("id"))
        stamp = re.sub(r"[^0-9T]", "", str(payload.get("stamp") or ""))
        source = VERSIONS_DIR / doc_id / f"{stamp}.json"
        if not source.exists():
            raise ValueError("version introuvable")
        current = DATA_DIR / f"{doc_id}.json"
        if current.exists():
            snapshot(doc_id, json.loads(current.read_text("utf-8")))
        temp = current.with_suffix(".tmp")
        temp.write_text(source.read_text("utf-8"), encoding="utf-8")
        temp.replace(current)
        self._send(200, {"ok": True, "restored": stamp})

    def handle_contribute(self) -> None:
        """
        Ajoute la contribution d'un étudiant à une séance partagée, sous verrou,
        sans écraser celle des autres. C'est le geste qui supprime les doublons :
        tout le monde écrit dans la MÊME séance.
        """
        payload = self._read_json()
        key = S.slugify(str(payload.get("sessionKey") or ""))
        if not key or key == "cours":
            raise ValueError("sessionKey manquant")
        # Bearer-authenticated contributors cannot impersonate another name via JSON.
        # Keep the payload fallback for the temporary Basic migration path.
        identity = self._identity()
        contributor = (identity or {}).get("displayName") if identity else str(payload.get("contributor") or "anonyme")
        contributor = str(contributor or "anonyme")
        kind = str(payload.get("kind") or "")
        # Durable clients replay envelopes after reconnect; acknowledge a replay
        # without applying it twice, independently of recording-id deduplication.
        receipt_key = str(self.headers.get("Idempotency-Key") or payload.get("idempotencyKey") or "").strip()
        with session_lock(key):
            session = load_session(key)
            if not self._resource_allowed(self._resource_owner(session)):
                self._deny_resource(); return
            if identity and not session.get("_ownerId"):
                session["_ownerId"] = identity.get("id")
            receipts = session.setdefault("_contributionReceipts", {})
            if receipt_key and receipt_key in receipts:
                summary = S.session_summary(session)
                summary["consensus"] = session.get("consensus")
                self._send(200, {"ok": True, "duplicate": True, "session": summary})
                return
            # Le cours, l'heure et le lieu viennent de l'agenda ICS déjà importé,
            # retrouvés par la clé — l'étudiant n'a rien à ressaisir. Le payload
            # peut aussi les porter (séance créée à la main, hors agenda).
            if not session.get("schedule"):
                session["schedule"] = payload.get("schedule") or load_schedule().get(key) or {}
            session = S.merge_contribution(session, contributor, kind, payload.get("payload") or {})
            if kind == "recording":
                # Le cache canonique est dérivé ici, sous le même verrou que la
                # contribution : aucun lecteur ne voit une fusion à moitié mise à jour.
                S.refresh_consensus(session)
            if receipt_key:
                receipts[receipt_key] = datetime.now(timezone.utc).isoformat()
                # Keep replay protection bounded per session.
                for old_key in list(receipts)[:-256]:
                    receipts.pop(old_key, None)
            store_session(key, session)
            summary = S.session_summary(session)
            # Le client remplace immédiatement son union locale par le canon ROVER.
            # On renvoie le cache dérivé (pas les audios ni les transcriptions brutes)
            # afin que la réponse reste bornée et que tous voient le même texte.
            summary["consensus"] = session.get("consensus")
        self._send(200, {"ok": True, "session": summary})

    def handle_session_notes(self) -> None:
        """
        Génère (ou régénère) l'unique document de la séance à partir de TOUT ce
        qui a été mis en commun : les paroles de tous les enregistrements et les
        pièces jointes de chacun. Un cours, un document — quelle que soit la
        personne qui déclenche la génération.
        """
        payload = self._read_json()
        key = S.slugify(str(payload.get("sessionKey") or ""))
        with session_lock(key):
            session = load_session(key)
            consensus = S.consensus_result(session)
            if session.get("consensus") is not consensus:
                session["consensus"] = consensus
                store_session(key, session)
        segments = consensus["segments"]
        attachments = [{"name": a["name"], "text": a["text"], "kind": a.get("kind")}
                       for a in (session.get("attachments") or [])]
        if not segments and not attachments:
            raise ValueError("séance vide : aucun enregistrement ni document")
        lang = (session.get("schedule") or {}).get("language") or payload.get("language", "auto")
        lang_code, lang_rule = resolve_language(lang, segments, attachments)
        speech = speech_indices(segments)
        api_key = self._api_key()
        if api_key == "":
            raise ValueError("MISTRAL_API_KEY absente : génération impossible")
        if len(speech) > WINDOW_SEGMENTS:
            doc, usage, latency = generate_windowed(api_key, segments, attachments, lang_rule, speech)
        else:
            usable = [(i, segments[i]) for i in speech]
            parts = []
            if usable:
                parts.append("TRANSCRIPTION DE LA SÉANCE\n"
                             + "\n".join(f"[s{i}] {seg['text']}" for i, seg in usable))
            if attachments:
                parts.append("DOCUMENTS FOURNIS\n"
                             + "\n\n".join(f"[a{i}] {a['name']}\n{a['text']}" for i, a in enumerate(attachments)))
            doc, usage, latency = mistral_chat(api_key, [
                {"role": "system", "content": SYSTEM_PROMPT.replace("{LANGUAGE_RULE}", lang_rule)},
                {"role": "user", "content": "\n\n".join(parts)}], max_tokens=8000)
        kept = []
        for block in doc.get("blocks") or []:
            if not isinstance(block, dict) or block.get("type") == "enrichment":
                continue
            fixed = coerce_block(block)
            if fixed is None:
                continue
            anchor = verify_anchor(fixed, segments, attachments)
            if anchor is None:
                continue
            fixed["anchor"] = anchor
            kept.append(fixed)
        document = {"title": doc.get("title", ""), "summary": doc.get("summary", ""),
                    "blocks": drop_hollow_headings(kept), "glossary": doc.get("glossary") or [],
                    "language": lang_code}
        with session_lock(key):
            session = load_session(key)
            session["doc"] = document
            store_session(key, session)
        self._send(200, {"ok": True, "doc": document,
                         "usedSegments": len(speech), "recordingCount": len(session.get("recordings") or []),
                         "consensus": consensus.get("stats") or {}})

    def handle_session_doc(self) -> None:
        """
        Enregistre le DOCUMENT d'une séance (retouches manuelles), sans toucher
        aux enregistrements ni aux pièces jointes des autres. C'est le seul
        écrit « note » sûr en multi-contributeur, en attendant le temps réel.
        """
        payload = self._read_json()
        key = S.slugify(str(payload.get("sessionKey") or ""))
        if not key or key == "cours":
            raise ValueError("sessionKey manquant")
        with session_lock(key):
            session = load_session(key)
            if not self._resource_allowed(self._resource_owner(session)):
                self._deny_resource(); return
            session["doc"] = payload.get("doc")
            store_session(key, session)
        self._send(200, {"ok": True})

    def handle_schedule_import(self) -> None:
        """
        Ingère l'agenda ICS d'un étudiant (Centrale, emlyon). Les événements
        deviennent des séances possibles, indexées par clé : les ICS de la promo
        fusionnent, et « quel cours maintenant ? » sait répondre.
        """
        payload = self._read_json()
        raw = payload.get("ics")
        source = str(payload.get("source") or "")
        if not raw and payload.get("url"):
            try:
                with urllib.request.urlopen(str(payload["url"]), timeout=20) as resp:
                    raw = resp.read().decode("utf-8", "replace")
            except Exception as exc:  # noqa: BLE001
                raise ValueError(f"téléchargement de l'agenda impossible : {exc}") from exc
        if not raw:
            raise ValueError("ni ics ni url fourni")
        book = S.schedule_from_ics(str(raw), source)
        with session_lock("__schedule__"):
            existing = load_schedule()
            existing.update(book)
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            SCHEDULE_FILE.write_text(json.dumps(existing, ensure_ascii=False, indent=2), "utf-8")
        self._send(200, {"ok": True, "added": len(book), "total": len(existing)})

    def handle_user_schedule_import(self) -> None:
        """Import already-parsed events into the caller's private calendar.

        This endpoint deliberately accepts no ICS text or URL, avoiding server-side
        fetching and ensuring one user's calendar cannot alter the shared book.
        Replays are idempotent by stable event id (or derived session key).
        """
        identity = self._identity()
        if not identity:
            self._demand_auth()
            return
        payload = self._read_json()
        events = payload.get("events")
        if not isinstance(events, list) or len(events) > 2000:
            raise ValueError("events doit être une liste (2000 maximum)")
        if payload.get("url") is not None or payload.get("ics") is not None:
            raise ValueError("URL/ICS interdits : envoyez uniquement les événements déjà analysés")
        source = str(payload.get("source") or "")[:120]
        imported: dict[str, dict[str, Any]] = {}
        for event in events:
            if not isinstance(event, dict):
                raise ValueError("événement invalide")
            course = str(event.get("course") or event.get("title") or "").strip()[:240]
            start = str(event.get("start") or "").strip()
            if not course or not start or event.get("url") or event.get("ics"):
                raise ValueError("événement: course et start requis; URL/ICS interdits")
            try:
                dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("start doit être une date ISO-8601") from exc
            if dt.tzinfo is None:
                raise ValueError("start doit inclure un fuseau")
            # The session key is the canonical identity, not the feed UID. UIDs
            # commonly change between exports/providers; using one as the map key
            # made the same class appear twice after importing another feed.
            session_id = S.session_key(course, dt)
            key = session_id
            if not key:
                raise ValueError("événement: id manquant")
            end_value = event.get("end")
            end_iso = None
            if end_value:
                try:
                    end_dt = datetime.fromisoformat(str(end_value).replace("Z", "+00:00"))
                except ValueError as exc:
                    raise ValueError("end doit être une date ISO-8601") from exc
                if end_dt.tzinfo is None or end_dt <= dt:
                    raise ValueError("end doit suivre start et inclure un fuseau")
                end_iso = end_dt.astimezone(timezone.utc).isoformat()
            imported[key] = {"eventId": key, "sessionKey": S.session_key(course, dt), "course": course,
                             "title": course, "start": dt.astimezone(timezone.utc).isoformat(),
                             "end": end_iso,
                             "location": str(event.get("location") or "")[:240], "source": source}
        user_id = str(identity.get("id") or "")
        with session_lock("__schedule-user-" + user_id):
            existing = load_user_schedule(user_id)
            added = sum(1 for key in imported if key not in existing)
            updated = len(imported) - added
            existing.update(imported)
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            user_schedule_path(user_id).write_text(json.dumps(existing, ensure_ascii=False, indent=2), "utf-8")
        self._send(200, {"ok": True, "added": added, "updated": updated, "total": len(existing)})

    def handle_save(self) -> None:
        payload = self._read_json()
        doc_id = safe_id(payload.get("id"))
        actor = self._identity()
        existing = DATA_DIR / f"{doc_id}.json"
        if existing.exists():
            current = json.loads(existing.read_text("utf-8"))
            if not self._resource_allowed(self._resource_owner(current)):
                self._deny_resource(); return
        if actor:
            payload["_ownerId"] = actor.get("id")
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        path = existing
        # Archive de l'état précédent AVANT d'écrire : régénérer des notes
        # remplaçait tout sans filet, et une mauvaise génération effaçait une
        # heure de cours sans possibilité de revenir en arrière.
        if path.exists():
            try:
                snapshot(doc_id, json.loads(path.read_text("utf-8")))
            except (ValueError, OSError):
                LOG.warning("archive impossible pour %s", doc_id)
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
    identity_auth.seed_admin_from_env()
    port = int(os.environ.get("PORT", "8765"))
    # 127.0.0.1 par défaut. Une écoute réseau n'est autorisée que si
    # AMPHI_PASSWORD est défini ; le tunnel Cloudflare reste l'accès public prévu.
    host = os.environ.get("AMPHI_HOST", "127.0.0.1")

    global AMPHI_PASSWORD
    AMPHI_PASSWORD = os.environ.get("AMPHI_PASSWORD", "")
    exposed = host not in ("127.0.0.1", "localhost")
    if exposed and not AMPHI_PASSWORD:
        raise SystemExit(
            "Refus de démarrer : AMPHI_HOST est ouvert sur le réseau mais "
            "AMPHI_PASSWORD est vide.\n"
            "Sans mot de passe, quiconque atteint l'adresse peut lire et "
            "supprimer toutes les notes.\n"
            "  AMPHI_PASSWORD='...' AMPHI_HOST=0.0.0.0 python3 apps/studio/server.py"
        )
    model = os.environ.get("AMPHI_ASR_MODEL", "mlx-community/whisper-large-v3-turbo")

    if ASR_AVAILABLE:
        StudioHandler.transcriber = Transcriber(model)
        LOG.info("chargement du modèle %s", model)
        StudioHandler.transcriber.warm_up()
    else:
        LOG.warning("Pas de moteur de transcription ici (%s).", ASR_IMPORT_ERROR)
        LOG.warning("Mode hébergement : notes, bibliothèque et recherche fonctionnent ; "
                    "l'enregistrement doit se faire depuis un poste équipé.")

    if os.environ.get("MISTRAL_API_KEY", "") == "":
        LOG.warning("MISTRAL_API_KEY absente — la transcription marchera, pas la génération de notes")

    server = ThreadingHTTPServer((host, port), StudioHandler)
    LOG.info("Amphi Studio prêt → http://127.0.0.1:%d", port)
    LOG.info("Mot de passe : %s", "activé" if AMPHI_PASSWORD else "aucun (accès local uniquement)")
    purged = purge_old_audio()
    if purged:
        LOG.info("audio expiré supprimé : %d fichier(s) de plus de %d jours", purged, AUDIO_RETENTION_DAYS)
    if host not in ("127.0.0.1", "localhost"):
        import socket

        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("192.0.2.1", 1))   # adresse de test, aucun paquet émis
            lan = probe.getsockname()[0]
        except OSError:
            lan = host
        finally:
            probe.close()
        LOG.warning("Ouvert sur le réseau → http://%s:%d (authentification activée)",
                    lan, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
