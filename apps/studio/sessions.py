"""
La séance partagée — le cœur d'Amphi (§3.3 du cahier des charges).

Une SÉANCE est un cours réel : une matière, une date, un créneau. Plusieurs
étudiants y rattachent leurs contributions — enregistrements, photos, PDF — et il
n'en sort qu'un seul document. C'est ce qui supprime les doublons : le doublon
n'existait que parce qu'on créait un fichier par personne.

Le rattachement se fait par une CLÉ dérivée du cours et de l'heure. Deux étudiants
de la même promo ont le même cours au même créneau dans leur agenda : leurs deux
calendriers — Centrale, emlyon — convergent tout seuls vers la même clé, sans
qu'ils aient rien à coordonner.

Ce module est volontairement PUR : il ne touche pas au disque. Les fonctions
prennent un dictionnaire de séance et en renvoient un autre. Toute l'écriture, le
verrouillage et le réseau restent dans server.py — c'est ce qui rend la fusion
testable sans serveur ni fichiers.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any

from rover import fuse_recordings

try:
    from zoneinfo import ZoneInfo
    # Les deux écoles sont à Lyon : une heure sans marqueur dans l'ICS est de
    # l'heure locale française, pas de l'UTC.
    _LOCAL_TZ: Any = ZoneInfo("Europe/Paris")
except Exception:  # noqa: BLE001 — sans base de fuseaux, on dégrade proprement
    _LOCAL_TZ = timezone.utc

# On arrondit l'heure de début à ce pas avant de fabriquer la clé. Deux agendas
# décrivant le même cours peuvent différer de deux ou trois minutes ; sans cet
# arrondi, ils fabriqueraient deux séances distinctes pour un seul cours.
SLOT_ROUNDING_MINUTES = 15


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return text or "cours"


def _round_slot(dt: datetime) -> datetime:
    minute = (dt.minute // SLOT_ROUNDING_MINUTES) * SLOT_ROUNDING_MINUTES
    return dt.replace(minute=minute, second=0, microsecond=0)


def session_key(course: str, start: datetime) -> str:
    """
    Clé stable et partagée d'une séance : matière + jour + créneau.

    Volontairement lisible (`machine-learning-20260916-1400`) : elle sert d'id de
    fichier, apparaît dans les journaux, et se retrouve à l'œil nu. Deux appels
    avec le même cours et une heure proche renvoient la même clé — c'est ce qui
    fait converger les contributions.
    """
    # La clé encode l'instant en UTC, pas l'affichage local. Les feeds Centrale
    # (déjà en Z), emlyon (heure de Paris) et une séance manuelle créée par le
    # client convergent ainsi même s'ils décrivent le fuseau différemment.
    if start.tzinfo is None:
        start = start.replace(tzinfo=_LOCAL_TZ)
    slot = _round_slot(start.astimezone(timezone.utc))
    return f"{slugify(course)}-{slot:%Y%m%d-%H%M}"


# ------------------------------------------------------------------ agenda ICS


def _unfold(raw: str) -> list[str]:
    """RFC 5545 : une ligne qui commence par une espace prolonge la précédente."""
    out: list[str] = []
    for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def _tz_from_params(params: str) -> Any:
    """Fuseau déclaré par un TZID=… sur la propriété, sinon l'heure locale."""
    for part in params.split(";"):
        if part.upper().startswith("TZID="):
            name = part[5:].strip()
            try:
                return ZoneInfo(name)
            except Exception:  # noqa: BLE001
                return _LOCAL_TZ
    return _LOCAL_TZ


def _ics_datetime(value: str, params: str) -> datetime | None:
    """
    Décode un DTSTART/DTEND vers de l'UTC. Trois formes coexistent dans les vrais
    agendas de la promo :
      - suffixe Z (Centrale) → déjà UTC ;
      - heure nue (emlyon) → heure locale, à convertir sinon un cours de 8 h
        s'afficherait à 10 h ;
      - TZID=… → le fuseau nommé.
    """
    value = value.strip()
    try:
        if value.endswith("Z"):
            return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        if "T" in value:
            naive = datetime.strptime(value, "%Y%m%dT%H%M%S")
            local = naive.replace(tzinfo=_tz_from_params(params))
            return local.astimezone(timezone.utc)
        if "VALUE=DATE" in params or len(value) == 8:
            return datetime.strptime(value, "%Y%m%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return None


def _unescape(text: str) -> str:
    return (text.replace("\\,", ",").replace("\\;", ";")
                .replace("\\n", " ").replace("\\N", " ").replace("\\\\", "\\").strip())


def parse_ics(raw: str) -> list[dict[str, Any]]:
    """
    Événements d'un flux ICS. Stdlib uniquement — pas de dépendance à installer
    sur la carte. On ne garde que ce qui sert : intitulé, début, fin, lieu, uid.
    """
    events: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in _unfold(raw):
        if line == "BEGIN:VEVENT":
            current = {}
            continue
        if line == "END:VEVENT":
            if current and current.get("start") and current.get("course"):
                events.append(current)
            current = None
            continue
        if current is None or ":" not in line:
            continue
        name, _, value = line.partition(":")
        key, _, params = name.partition(";")
        key = key.upper()
        if key == "SUMMARY":
            current["course"] = _unescape(value)
        elif key == "LOCATION":
            current["location"] = _unescape(value)
        elif key == "UID":
            current["uid"] = value.strip()
        elif key == "DTSTART":
            current["start"] = _ics_datetime(value, params)
        elif key == "DTEND":
            current["end"] = _ics_datetime(value, params)
    return events


def schedule_from_ics(raw: str, source: str = "") -> dict[str, dict[str, Any]]:
    """
    Transforme un ICS en carnet de séances, indexé par clé. C'est ce carnet, une
    fois les ICS de la promo fusionnés, qui alimente « quel cours maintenant ? ».
    """
    book: dict[str, dict[str, Any]] = {}
    for event in parse_ics(raw):
        start: datetime = event["start"]
        key = session_key(event["course"], start)
        book[key] = {
            "sessionKey": key,
            "course": event["course"],
            "title": event["course"],
            "start": start.astimezone(timezone.utc).isoformat(),
            "end": (event["end"].astimezone(timezone.utc).isoformat()
                    if event.get("end") else None),
            "location": event.get("location", ""),
            "source": source,
        }
    return book


# --------------------------------------------------------------- fusion


def blank_session(key: str, schedule: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "sessionKey": key,
        "schedule": schedule or {},
        "contributors": [],
        "recordings": [],
        "attachments": [],
        "doc": None,
        "savedAt": None,
    }


def _text_hash(text: str) -> str:
    return hashlib.sha1((text or "").strip().encode("utf-8")).hexdigest()[:16]


def merge_contribution(
    session: dict[str, Any], contributor: str, kind: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """
    Ajoute la contribution d'un étudiant à une séance, SANS jamais écraser celle
    d'un autre. Fonction pure : elle renvoie la séance modifiée, l'appelant
    s'occupe de l'écrire sous verrou.

    Idempotente par construction : un enregistrement est reconnu à son id, une
    pièce jointe à (auteur, nom, empreinte du texte). Une contribution rejouée —
    reprise réseau, double-clic — ne crée donc pas de doublon.
    """
    contributor = (contributor or "anonyme").strip() or "anonyme"
    session.setdefault("recordings", [])
    session.setdefault("attachments", [])
    session.setdefault("contributors", [])

    if contributor not in session["contributors"]:
        session["contributors"].append(contributor)

    if kind == "recording":
        rec_id = str(payload.get("id") or "")
        if not rec_id:
            rec_id = f"{slugify(contributor)}-{_text_hash(str(payload.get('startMs')))}"
        existing = next((r for r in session["recordings"] if r.get("id") == rec_id), None)
        record = {
            "id": rec_id,
            "by": contributor,
            "mime": payload.get("mime"),
            "url": payload.get("url"),
            "startMs": payload.get("startMs", 0),
            "endMs": payload.get("endMs"),
            "segments": payload.get("segments") or [],
            "addedAt": payload.get("addedAt"),
            "clock": payload.get("clock"),
            "quality": payload.get("quality"),
        }
        if existing:
            session["recordings"][session["recordings"].index(existing)] = record
        else:
            session["recordings"].append(record)
        # Le cache ROVER dépend mot pour mot des enregistrements. Une reprise réseau
        # peut remplacer un chunk existant : dans les deux cas il doit être recalculé.
        session.pop("consensus", None)

    elif kind in ("photo", "pdf", "office", "text", "attachment"):
        h = _text_hash(str(payload.get("text") or ""))
        name = str(payload.get("name") or "sans-nom")
        already = any(
            a.get("by") == contributor and a.get("name") == name and a.get("hash") == h
            for a in session["attachments"]
        )
        if not already:
            session["attachments"].append({
                "by": contributor,
                "name": name,
                "kind": payload.get("kind") or ("text" if kind == "text" else kind),
                "text": payload.get("text") or "",
                "preview": payload.get("preview"),
                "hash": h,
                "addedAt": payload.get("addedAt"),
            })
    else:
        raise ValueError(f"type de contribution inconnu : {kind!r}")

    return session


CONSENSUS_VERSION = "rover-1"


def _recordings_fingerprint(recordings: list[dict[str, Any]]) -> str:
    """Empreinte du contenu qui influence ROVER — jamais des octets audio."""
    relevant = [{
        "id": rec.get("id"), "by": rec.get("by"),
        "startMs": rec.get("startMs"), "endMs": rec.get("endMs"),
        "clock": rec.get("clock"), "quality": rec.get("quality"),
        "segments": rec.get("segments") or [],
    } for rec in recordings]
    raw = json.dumps(relevant, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _single_stream_segments(recordings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Chemin historique exact : un micro ne doit pas changer en activant ROVER."""
    out: list[dict[str, Any]] = []
    for rec in recordings:
        for seg in rec.get("segments") or []:
            item = dict(seg)
            item["by"] = rec.get("by")
            item["rec"] = rec.get("id")
            out.append(item)
    out.sort(key=lambda segment: segment.get("startMs", 0))
    return out


def consensus_result(session: dict[str, Any]) -> dict[str, Any]:
    """
    Transcription canonique et statistiques, avec cache versionné dans la séance.

    Les enregistrements originaux restent l'unique source d'audit. Le cache est
    dérivé et jetable : un ancien JSON sans ``consensus`` fonctionne tel quel.
    """
    recordings = session.get("recordings") or []
    fingerprint = _recordings_fingerprint(recordings)
    cached = session.get("consensus") or {}
    if (cached.get("algorithm") == CONSENSUS_VERSION and
            cached.get("fingerprint") == fingerprint and
            isinstance(cached.get("segments"), list)):
        return cached

    usable = [rec for rec in recordings if rec.get("segments")]
    if len(usable) <= 1:
        segments = _single_stream_segments(recordings)
        result = {
            "segments": segments,
            "stats": {"mode": "single" if usable else "empty",
                      "streamCount": len(recordings), "aligned": len(usable),
                      "excluded": [str(rec.get("id") or "") for rec in recordings
                                   if not rec.get("segments")], "disputed": 0},
        }
    else:
        fused = fuse_recordings(recordings)
        segments = fused["segments"]
        for index, segment in enumerate(segments):
            segment.setdefault("id", "c%05d" % index)
        result = {"segments": segments, "stats": fused["stats"]}

    return {"algorithm": CONSENSUS_VERSION, "fingerprint": fingerprint, **result}


def refresh_consensus(session: dict[str, Any]) -> dict[str, Any]:
    """Actualise explicitement le cache dérivé et rend la séance pour chaînage."""
    session["consensus"] = consensus_result(session)
    return session


def merged_segments(session: dict[str, Any]) -> list[dict[str, Any]]:
    """Paroles canoniques : flux unique intact, plusieurs micros votés par ROVER."""
    return consensus_result(session)["segments"]


def session_summary(session: dict[str, Any]) -> dict[str, Any]:
    """Vue compacte pour la liste « séances ouvertes » et l'écran de la séance."""
    recordings = session.get("recordings") or []
    total_ms = max((r.get("endMs") or 0) for r in recordings) if recordings else 0
    sched = session.get("schedule") or {}
    consensus = consensus_result(session)
    return {
        "sessionKey": session.get("sessionKey"),
        "course": sched.get("course") or (session.get("doc") or {}).get("title"),
        "title": sched.get("title") or (session.get("doc") or {}).get("title"),
        "start": sched.get("start"),
        "location": sched.get("location", ""),
        "contributors": session.get("contributors") or [],
        "recordingCount": len(recordings),
        "attachmentCount": len(session.get("attachments") or []),
        "durationMs": total_ms,
        "hasNotes": bool(session.get("doc")),
        "consensus": consensus.get("stats") or {},
        "savedAt": session.get("savedAt"),
    }
