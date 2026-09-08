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
import hmac
import json
import logging
from datetime import datetime, timezone
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

# Le moteur de transcription tire numpy, MLX et ffmpeg. Sur la machine qui
# héberge l'app pour la promo — un Tinker Board, un Pi — rien de tout ça n'est
# installable ni utile : elle sert des pages et appelle des API, la
# transcription se fait ailleurs. L'import est donc facultatif.
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
                self._send(200, {"ok": True, "available": False, "warm": False,
                                 "model": "aucun moteur local", "batteryPercent": None,
                                 "reason": ASR_IMPORT_ERROR})
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
    choice = payload["choices"][0]
    content = choice["message"]["content"]
    if choice.get("finish_reason") == "length":
        LOG.warning("réponse coupée au plafond de tokens — récupération partielle")
    return loads_lenient(content), payload.get("usage", {}), latency_ms


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


# Au-delà, un seul appel ne tient plus : le JSON est coupé au milieu et tout
# l'appel est perdu. Un cours d'une heure fait couramment 600 segments.
WINDOW_SEGMENTS = 130
WINDOW_OVERLAP = 4

WINDOW_PROMPT = """Tu produis les notes d'une PARTIE d'un cours — pas du cours entier.

{LANGUAGE_RULE}

Mêmes règles que d'habitude : chaque bloc cite ses sources dans `sourceSegmentIds`,
rien d'inventé, pas de section creuse, pas de bloc tiré d'une phrase administrative.
Les formules dictées deviennent des blocs "formula" en LaTeX ; les symboles dans le
texte s'écrivent entre $...$.

STRUCTURE — c'est ce qui distingue des notes d'une transcription reformatée.
Ouvre par un titre de section (heading, level 2) qui nomme ce dont il est question,
et découpe la partie en une à trois sections. Dès qu'une idée se décline, utilise des
puces plutôt qu'un paragraphe : trois points en liste se relisent, un paragraphe de
huit lignes ne se relit pas. Un terme technique introduit devient une "definition".
Un enchaînement de paragraphes sans titre ni liste est un échec.

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


def generate_windowed(
    api_key: str, segments: list[dict[str, Any]], attachments: list[dict[str, Any]],
    language_rule: str,
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

    context = ""
    if attachments:
        context = "\n\nDOCUMENTS FOURNIS\n" + "\n\n".join(
            f"[a{i}] {a['name']}\n{a['text'][:2500]}" for i, a in enumerate(attachments)
        )

    starts = range(0, len(segments), WINDOW_SEGMENTS - WINDOW_OVERLAP)
    windows = [(i, min(i + WINDOW_SEGMENTS, len(segments))) for i in starts]
    windows = [w for w in windows if w[1] > w[0]]
    LOG.info("cours long : %d segments → %d fenêtres", len(segments), len(windows))

    for n, (lo, hi) in enumerate(windows, 1):
        body = "\n".join(f"[s{i}] {segments[i]['text']}" for i in range(lo, hi))
        header = f"PARTIE {n} SUR {len(windows)} DU COURS\n"
        try:
            doc, u, _ = mistral_chat(
                api_key,
                [
                    {"role": "system", "content": WINDOW_PROMPT.replace("{LANGUAGE_RULE}", language_rule)},
                    {"role": "user", "content": header + body + (context if n == 1 else "")},
                ],
                max_tokens=8000,
            )
        except Exception:  # noqa: BLE001 — une fenêtre ratée ne doit pas perdre les autres
            LOG.exception("fenêtre %d/%d", n, len(windows))
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
                    found = locate_in_window(fixed, segments, lo, hi)
                    if found:
                        fixed["sourceSegmentIds"] = found
                blocks.append(fixed)

    # En-tête et glossaire à partir des blocs, pas de la transcription entière.
    outline = "\n".join(
        line for line in (
            f"- {b.get('text') or b.get('term') or b.get('caption') or ' '.join(b.get('items') or [])}"[:180]
            for b in blocks
        ) if line.strip(" -")
    )[:14000]
    head: dict[str, Any] = {}
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
                "course": (payload.get("course") or "").strip(),
                "chapter": (payload.get("chapter") or "").strip(),
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

    def _authorized(self) -> bool:
        """Authentification HTTP Basic. Comparaison à temps constant."""
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

    def _demand_auth(self) -> None:
        body = b'{"error":"authentification requise"}'
        self.send_response(401)
        self.send_header("WWW-Authenticate", f'Basic realm="{REALM}", charset="UTF-8"')
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------------ GET

    def do_GET(self) -> None:  # noqa: N802
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
            target = AUDIO_DIR / safe_id(parts[0]) / re.sub(r"[^0-9a-zA-Z.]", "", parts[1])
            self._send_file(target, "audio/webm" if target.suffix == ".webm" else "audio/mp4",
                            cache="private, max-age=86400")
            return
        if self.path.startswith("/versions"):
            from urllib.parse import parse_qs, urlparse

            doc_id = safe_id((parse_qs(urlparse(self.path).query).get("id") or [""])[0])
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
            from urllib.parse import parse_qs, urlparse

            course = (parse_qs(urlparse(self.path).query).get("course") or [""])[0]
            self._send(200, {"course": course, "terms": lexicon_for_course(course)})
            return
        if self.path.startswith("/search"):
            from urllib.parse import parse_qs, urlparse
            q = (parse_qs(urlparse(self.path).query).get("q") or [""])[0]
            self._send(200, {"query": q, "results": search_documents(q)})
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
        }
        # `self.path` contient la chaîne de requête : /audio?id=… ne matchait
        # aucune route et repartait en 404 sans explication.
        from urllib.parse import urlparse

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

        # Les indices restent GLOBAUX : un bloc doit pouvoir citer [s412] même
        # si les segments 400 à 411 ont été écartés comme bruit.
        usable = [(i, s) for i, s in enumerate(segments)
                  if float(s.get("avgConfidence") or 0) >= MIN_SEGMENT_CONFIDENCE]
        dropped = len(segments) - len(usable)
        if dropped:
            LOG.info("bruit écarté avant génération : %d segments sur %d", dropped, len(segments))

        parts = []
        if usable:
            parts.append("TRANSCRIPTION DE LA SÉANCE\n"
                         + "\n".join(f"[s{i}] {s['text']}" for i, s in usable))
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
                if len(segments) > WINDOW_SEGMENTS:
                    doc, usage, latency_ms = generate_windowed(api_key, segments, attachments, lang_rule)
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

    def handle_audio(self) -> None:
        """
        Reçoit une partie d'enregistrement et la garde sur disque.

        Sans ça, rouvrir une séance donnait le texte mais plus le son : les
        horodatages restaient affichés en ne renvoyant nulle part, ce qui vidait
        de sens la traçabilité qui est le principe du produit.
        """
        from urllib.parse import parse_qs, urlparse

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

    def handle_save(self) -> None:
        payload = self._read_json()
        doc_id = safe_id(payload.get("id"))
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        path = DATA_DIR / f"{doc_id}.json"
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
    port = int(os.environ.get("PORT", "8765"))
    # 127.0.0.1 par défaut : le studio n'a AUCUNE authentification. L'ouvrir sur
    # le réseau, c'est offrir la lecture et la suppression de toutes les notes à
    # quiconque partage le wifi. Réservé au partage ponctuel entre camarades.
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
        LOG.warning("Ouvert sur le réseau → http://%s:%d", lan, port)
        LOG.warning("AUCUNE authentification : n'importe qui sur ce réseau peut lire "
                    "et supprimer toutes les notes. À couper après usage.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
