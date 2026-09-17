"""
ROVER déterministe pour les transcriptions partagées d'Amphi.

Le module est volontairement pur et limité à la bibliothèque standard. Il ne
modifie jamais les enregistrements reçus : la valeur ``originals`` du résultat
est une copie profonde, utile pour revenir à chaque transcription individuelle.
"""
from __future__ import annotations

import copy
import difflib
import math
import re
import statistics
import unicodedata
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


_TOKEN_RE = re.compile(r"\w+(?:['’\-]\w+)*|[^\w\s]", re.UNICODE)
_PUNCT_LEFT = set(",.;:!?%)]}»")
_PUNCT_RIGHT = set("([{«")
_DELETION = "\0"
_MAX_DRIFT = 0.03                 # ±3 %, assez pour deux horloges grand public
_TIME_GATE_MS = 1_800.0
_MAX_SEGMENT_MS = 15_000.0
_SPLIT_GAP_MS = 700.0


def _number(value: Any, default: float) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _bounded(value: Any, default: float = 0.5) -> float:
    return max(0.0, min(0.99, _number(value, default)))


def _normalise(text: Any) -> str:
    value = unicodedata.normalize("NFKD", str(text or "").casefold())
    value = "".join(char for char in value if not unicodedata.combining(char))
    return re.sub(r"[^\w]+", "", value, flags=re.UNICODE)


def _tokens(text: Any) -> List[str]:
    return [token for token in _TOKEN_RE.findall(str(text or "")) if token.strip()]


def _join(words: Sequence[str]) -> str:
    """Recoud les mots sans espace artificielle avant la ponctuation."""
    out = ""
    for word in words:
        if not out:
            out = word
        elif word in _PUNCT_LEFT or word.startswith(("'", "’")):
            out += word
        elif out[-1:] in _PUNCT_RIGHT:
            out += word
        else:
            out += " " + word
    return out.strip()


def _clock_transform(recording: Dict[str, Any]) -> Tuple[float, float]:
    """Lit les formes simples de ``clock`` sans imposer un nouveau schéma."""
    clock = recording.get("clock")
    if isinstance(clock, (int, float)):
        return 1.0, _number(clock, 0.0)
    if not isinstance(clock, dict):
        return 1.0, 0.0
    # Le modèle partagé officiel est {a, b}. Les anciens prototypes utilisaient
    # rate/offsetMs ou driftPpm : on lit les trois sans migration destructive.
    offset = _number(clock.get("b", clock.get("offsetMs", clock.get("offset", 0.0))), 0.0)
    if "a" in clock:
        rate = _number(clock.get("a"), 1.0)
    elif "rate" in clock:
        rate = _number(clock.get("rate"), 1.0)
    else:
        rate = 1.0 + _number(clock.get("driftPpm"), 0.0) / 1_000_000.0
    return max(1.0 - _MAX_DRIFT, min(1.0 + _MAX_DRIFT, rate)), offset


def _segment_words(segment: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Mots natifs, ou interpolation fidèle pour les anciens segments."""
    start = _number(segment.get("startMs"), 0.0)
    end = max(start, _number(segment.get("endMs"), start))
    confidence = _bounded(segment.get("avgConfidence"), 0.5)
    supplied = segment.get("words")
    result = []
    if isinstance(supplied, list):
        for raw in supplied:
            if not isinstance(raw, dict) or not str(raw.get("text", raw.get("word", ""))).strip():
                continue
            word_start = _number(raw.get("startMs"), start)
            word_end = max(word_start, _number(raw.get("endMs"), word_start))
            result.append({
                "text": str(raw.get("text", raw.get("word", ""))).strip(),
                "start": word_start,
                "end": word_end,
                "confidence": _bounded(raw.get("confidence"), confidence),
            })
    if result:
        return result

    fallback = _tokens(segment.get("text"))
    if not fallback:
        return []
    duration = max(end - start, 1.0)
    for index, text in enumerate(fallback):
        result.append({
            "text": text,
            "start": start + duration * index / len(fallback),
            "end": start + duration * (index + 1) / len(fallback),
            "confidence": confidence,
        })
    return result


def _flatten(recording: Dict[str, Any], order: int) -> Optional[Dict[str, Any]]:
    rec_id = str(recording.get("id") or "recording-%06d" % order)
    contributor = str(recording.get("by") or "anonyme")
    clock_rate, clock_offset = _clock_transform(recording)
    words = []
    for segment_index, segment in enumerate(recording.get("segments") or []):
        if not isinstance(segment, dict):
            continue
        incoming = _segment_words(segment)
        # Whisper recopie parfois un à quatre mots au bord de deux chunks. On ne
        # les laisse pas devenir deux votes distincts dans le treillis.
        overlap = 0
        if words and incoming:
            previous_norm = [_normalise(w["text"]) for w in words]
            incoming_norm = [_normalise(w["text"]) for w in incoming]
            for size in range(min(8, len(words), len(incoming)), 0, -1):
                close = incoming[0]["start"] <= words[-1]["end"] + 1_500.0
                if close and previous_norm[-size:] == incoming_norm[:size]:
                    overlap = size
                    break
        for word_index, item in enumerate(incoming[overlap:], start=overlap):
            start = item["start"] * clock_rate + clock_offset
            end = item["end"] * clock_rate + clock_offset
            words.append({
                "text": item["text"], "norm": _normalise(item["text"]),
                "start": start, "end": max(start, end),
                "confidence": item["confidence"], "segment": segment_index,
                "word": word_index,
            })
    words = [word for word in words if word["norm"] or word["text"] in _PUNCT_LEFT]
    if not words:
        return None

    supplied_quality = recording.get("quality")
    mean_confidence = sum(word["confidence"] for word in words) / len(words)
    if isinstance(supplied_quality, dict):
        direct = supplied_quality.get("score", supplied_quality.get("confidence"))
        if direct is not None:
            supplied_quality = direct
        else:
            # Même pondération que packages/consensus : signal, confiance, trous.
            snr = max(0.0, min(1.0, _number(supplied_quality.get("snr"), 0.0) / 30.0))
            confidence = _bounded(supplied_quality.get("meanConfidence"), mean_confidence)
            gap_ratio = max(0.0, min(1.0, _number(supplied_quality.get("gapRatio"), 0.0)))
            supplied_quality = 0.5 * snr + 0.3 * confidence + 0.2 * (1.0 - gap_ratio)
    # Une qualité explicite prime ; sinon confiance et densité donnent un score
    # stable, sans pénaliser brutalement les anciens documents (confiance 0,5).
    if supplied_quality is None:
        quality = max(0.20, min(0.99, 0.75 * mean_confidence + 0.25 * min(1.0, len(words) / 20.0)))
    else:
        quality = max(0.05, _bounded(supplied_quality, mean_confidence))
    return {
        "id": rec_id, "by": contributor, "order": order, "words": words,
        "quality": quality, "start": words[0]["start"], "end": words[-1]["end"],
        "affine": (1.0, 0.0),
    }


def _unique_ngrams(words: Sequence[Dict[str, Any]]) -> Dict[Tuple[str, ...], float]:
    found = {}  # type: Dict[Tuple[str, ...], List[float]]
    norms = [word["norm"] for word in words]
    for size in range(2, 5):
        for index in range(len(words) - size + 1):
            gram = tuple(norms[index:index + size])
            if not all(gram):
                continue
            midpoint = (words[index]["start"] + words[index + size - 1]["end"]) / 2.0
            found.setdefault(gram, []).append(midpoint)
    return {gram: positions[0] for gram, positions in found.items() if len(positions) == 1}


def _affine_to_pivot(stream: Dict[str, Any], pivot: Dict[str, Any]) -> Tuple[float, float, int]:
    """Décalage et dérive robustes à partir d'ancres lexicales uniques."""
    left = _unique_ngrams(stream["words"])
    right = _unique_ngrams(pivot["words"])
    # Les ancres longues sont plus discriminantes. Une même position peut être
    # décrite par plusieurs sous-grammes : on la déduplique avant l'estimation.
    pairs = sorted(set((left[gram], right[gram]) for gram in left.keys() & right.keys()))
    if not pairs:
        return 1.0, 0.0, 0
    slopes = []
    for i, (x1, y1) in enumerate(pairs):
        for x2, y2 in pairs[i + 1:]:
            if abs(x2 - x1) >= 2_000.0:
                slopes.append((y2 - y1) / (x2 - x1))
    slope = statistics.median(slopes) if slopes else 1.0
    slope = max(1.0 - _MAX_DRIFT, min(1.0 + _MAX_DRIFT, slope))
    offsets = [target - slope * source for source, target in pairs]
    offset = statistics.median(offsets)
    residuals = [abs(target - (slope * source + offset)) for source, target in pairs]
    if len(residuals) >= 3:
        median_residual = statistics.median(residuals)
        limit = max(350.0, median_residual * 3.0)
        inliers = [pair for pair, residual in zip(pairs, residuals) if residual <= limit]
        if inliers:
            offset = statistics.median(target - slope * source for source, target in inliers)
            pairs = inliers
    return slope, offset, len(pairs)


def _apply_affine(stream: Dict[str, Any], slope: float, offset: float) -> None:
    stream["affine"] = (slope, offset)
    for word in stream["words"]:
        word["start"] = slope * word["start"] + offset
        word["end"] = slope * word["end"] + offset
    stream["start"] = stream["words"][0]["start"]
    stream["end"] = stream["words"][-1]["end"]


def _slot_reference(slot: Dict[str, Any]) -> Dict[str, Any]:
    observations = slot["observations"]
    groups = {}  # type: Dict[str, List[Dict[str, Any]]]
    for observation in observations:
        groups.setdefault(observation["norm"], []).append(observation)
    ranked = sorted(
        groups.items(),
        key=lambda pair: (-sum(item["weight"] for item in pair[1]), pair[0]),
    )
    chosen = ranked[0][1]
    return min(chosen, key=lambda item: (-item["weight"], item["streamOrder"], item["text"]))


def _compatible(slot: Dict[str, Any], word: Dict[str, Any]) -> bool:
    reference = _slot_reference(slot)
    middle_a = (reference["start"] + reference["end"]) / 2.0
    middle_b = (word["start"] + word["end"]) / 2.0
    return abs(middle_a - middle_b) <= _TIME_GATE_MS


def _match_cost(slot: Dict[str, Any], word: Dict[str, Any]) -> float:
    reference = _slot_reference(slot)
    delta = abs((reference["start"] + reference["end"] - word["start"] - word["end"]) / 2.0)
    same = reference["norm"] == word["norm"]
    # Une substitution proche dans le temps coûte moins qu'une paire de gaps ;
    # hors de la porte temporelle, elle est interdite.
    return (0.0 if same else 1.15) + min(0.65, delta / _TIME_GATE_MS * 0.65)


def _align_small(
    slots: Sequence[Dict[str, Any]], words: Sequence[Dict[str, Any]],
    slot_base: int = 0, word_base: int = 0, ignore_time: bool = False,
) -> List[Tuple[str, int, int]]:
    """DP exact sur un petit intervalle, jamais sur tout un cours de deux heures."""
    rows, cols = len(slots), len(words)
    inf = float("inf")
    cost = [[inf] * (cols + 1) for _ in range(rows + 1)]
    path = [[""] * (cols + 1) for _ in range(rows + 1)]
    cost[0][0] = 0.0
    for i in range(1, rows + 1):
        cost[i][0], path[i][0] = cost[i - 1][0] + 1.0, "D"
    for j in range(1, cols + 1):
        cost[0][j], path[0][j] = cost[0][j - 1] + 1.0, "I"
    priority = {"M": 0, "D": 1, "I": 2}
    for i in range(1, rows + 1):
        for j in range(1, cols + 1):
            choices = [(cost[i - 1][j] + 1.0, "D"), (cost[i][j - 1] + 1.0, "I")]
            if ignore_time or _compatible(slots[i - 1], words[j - 1]):
                choices.append((cost[i - 1][j - 1] + _match_cost(slots[i - 1], words[j - 1]), "M"))
            best = min(choices, key=lambda item: (round(item[0], 9), priority[item[1]]))
            cost[i][j], path[i][j] = best
    operations = []
    i, j = rows, cols
    while i or j:
        operation = path[i][j]
        operations.append((operation, slot_base + i - 1, word_base + j - 1))
        if operation == "M":
            i, j = i - 1, j - 1
        elif operation == "D":
            i -= 1
        else:
            j -= 1
    operations.reverse()
    return operations


def _align(
    slots: Sequence[Dict[str, Any]], words: Sequence[Dict[str, Any]], ignore_time: bool = False,
) -> List[Tuple[str, int, int]]:
    """
    Aligne un cours entier sans matrice quadratique géante.

    SequenceMatcher trouve les longues îles lexicales en quasi-linéaire ; seuls les
    petits trous entre elles passent par le DP exact. Un mauvais micro totalement
    divergent ne peut donc pas faire exploser les 2 Go du Tinker Board.
    """
    left = [_slot_reference(slot)["norm"] for slot in slots]
    right = [word["norm"] for word in words]
    matcher = difflib.SequenceMatcher(None, left, right, autojunk=False)
    operations = []  # type: List[Tuple[str, int, int]]
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            operations.extend(("M", i1 + offset, j1 + offset) for offset in range(i2 - i1))
        elif tag == "delete":
            operations.extend(("D", index, j1 - 1) for index in range(i1, i2))
        elif tag == "insert":
            operations.extend(("I", i1 - 1, index) for index in range(j1, j2))
        elif (i2 - i1) * (j2 - j1) <= 50_000:
            operations.extend(_align_small(slots[i1:i2], words[j1:j2], i1, j1, ignore_time))
        else:
            # Intervalle pathologique sans îlot commun : on préserve tout plutôt
            # que de fabriquer des correspondances ou d'allouer une matrice énorme.
            operations.extend(("D", index, j1 - 1) for index in range(i1, i2))
            operations.extend(("I", i2 - 1, index) for index in range(j1, j2))
    return operations


def _observation(word: Dict[str, Any], stream: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "text": word["text"], "norm": word["norm"], "start": word["start"], "end": word["end"],
        "confidence": word["confidence"], "weight": word["confidence"] * stream["quality"],
        "by": stream["by"], "rec": stream["id"], "streamOrder": stream["order"],
        "segment": word.get("segment"), "word": word.get("word"),
    }


def _add_stream(slots: List[Dict[str, Any]], stream: Dict[str, Any]) -> List[Dict[str, Any]]:
    operations = _align(slots, stream["words"], ignore_time=not stream.get("anchors"))
    out = []
    for operation, slot_index, word_index in operations:
        if operation == "M":
            slot = slots[slot_index]
            slot["observations"].append(_observation(stream["words"][word_index], stream))
            out.append(slot)
        elif operation == "D":
            out.append(slots[slot_index])
        else:
            out.append({"observations": [_observation(stream["words"][word_index], stream)]})
    return out


def _vote(slot: Dict[str, Any], streams: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    alternatives = {}  # type: Dict[str, Dict[str, Dict[str, Any]]]
    middle_values = [(item["start"] + item["end"]) / 2.0 for item in slot["observations"]]
    middle = statistics.median(middle_values)
    present = set()
    for item in slot["observations"]:
        present.add(item["by"])
        contributor = alternatives.setdefault(item["norm"], {})
        current = contributor.get(item["by"])
        # Au plus un vote d'une personne pour une alternative dans un slot.
        if current is None or (item["weight"], -item["streamOrder"]) > (current["weight"], -current["streamOrder"]):
            contributor[item["by"]] = item
    for stream in streams:
        if stream["by"] not in present and stream["start"] - 250.0 <= middle <= stream["end"] + 250.0:
            deleted = {
                "text": "", "norm": _DELETION, "start": middle, "end": middle,
                "confidence": 1.0, "weight": stream["quality"], "by": stream["by"],
                "rec": stream["id"], "streamOrder": stream["order"],
            }
            contributor = alternatives.setdefault(_DELETION, {})
            current = contributor.get(stream["by"])
            if current is None or deleted["weight"] > current["weight"]:
                contributor[stream["by"]] = deleted

    totals = {key: sum(item["weight"] for item in voters.values()) for key, voters in alternatives.items()}
    ranking = sorted(totals, key=lambda key: (-totals[key], key == _DELETION, key))
    winner_key = ranking[0]
    voters = alternatives[winner_key]
    winner = min(voters.values(), key=lambda item: (-item["weight"], item["streamOrder"], item["text"]))
    total = sum(totals.values()) or 1.0
    score = totals[winner_key] / total
    second = totals[ranking[1]] / total if len(ranking) > 1 else 0.0
    disputed = len(ranking) > 1 and (score < 0.67 or second >= 0.25)

    rendered = []
    for key in ranking:
        voters_for_key = alternatives[key]
        representative = min(voters_for_key.values(), key=lambda item: (-item["weight"], item["streamOrder"], item["text"]))
        rendered.append({
            "text": "" if key == _DELETION else representative["text"],
            "isDeletion": key == _DELETION,
            "weight": round(totals[key], 6),
            "by": sorted(voters_for_key),
            "rec": sorted({item["rec"] for item in voters_for_key.values()}),
        })
    all_sources = sorted(
        {(item["by"], item["rec"]) for item in slot["observations"]},
        key=lambda item: (item[0], item[1]),
    )
    all_support = sorted(
        ({"by": item["by"], "rec": item["rec"], "segment": item.get("segment"),
          "word": item.get("word"), "startMs": round(item["start"], 1),
          "endMs": round(item["end"], 1)} for item in slot["observations"]),
        key=lambda item: (item["by"], item["rec"], item["segment"] or 0, item["word"] or 0),
    )
    return {
        "deleted": winner_key == _DELETION,
        "text": winner["text"],
        "start": statistics.median(item["start"] for item in voters.values()),
        "end": statistics.median(item["end"] for item in voters.values()),
        "confidence": sum(item["confidence"] * item["weight"] for item in voters.values()) /
                      max(1e-9, sum(item["weight"] for item in voters.values())),
        "score": score, "disputed": disputed, "alternatives": rendered,
        "sources": all_sources, "support": all_support,
    }


def _canonical_segments(voted: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    kept = [word for word in voted if not word["deleted"]]
    if not kept:
        return []
    groups = []  # type: List[List[Dict[str, Any]]]
    current = []  # type: List[Dict[str, Any]]
    for word in kept:
        split = bool(current) and (
            word["start"] - current[-1]["end"] > _SPLIT_GAP_MS or
            max(current[-1]["end"], word["end"]) - current[0]["start"] > _MAX_SEGMENT_MS
        )
        if split:
            groups.append(current)
            current = []
        current.append(word)
    if current:
        groups.append(current)

    segments = []
    for group in groups:
        sources = sorted({source for word in group for source in word["sources"]}, key=lambda item: (item[0], item[1]))
        variants = []
        for word in group:
            if word["disputed"]:
                variants.append({
                    "startMs": round(word["start"], 1), "endMs": round(word["end"], 1),
                    "chosen": word["text"], "alternatives": word["alternatives"],
                })
        weights = [max(0.01, word["end"] - word["start"]) for word in group]
        denominator = sum(weights)
        support = []
        seen_support = set()
        for word in group:
            for item in word["support"]:
                key = (item["by"], item["rec"], item["segment"], item["word"])
                if key not in seen_support:
                    seen_support.add(key)
                    support.append(item)
        segment = {
            "text": _join([word["text"] for word in group]),
            "startMs": round(min(word["start"] for word in group), 1),
            "endMs": round(max(word["end"] for word in group), 1),
            "avgConfidence": round(sum(word["confidence"] * weight for word, weight in zip(group, weights)) / denominator, 4),
            "consensusScore": round(sum(word["score"] * weight for word, weight in zip(group, weights)) / denominator, 4),
            "isDisputed": any(word["disputed"] for word in group),
            "variants": variants,
            "sources": [{"by": by, "rec": rec} for by, rec in sources],
            "support": support,
            "by": sorted({by for by, _ in sources}),
            "rec": sorted({rec for _, rec in sources}),
        }
        segments.append(segment)

    # Une insertion rejetée par le vote est une suppression gagnante dans le
    # treillis. Elle n'a donc pas de mot canonique auquel accrocher sa variante ;
    # on la rattache au segment temporel le plus proche pour ne pas la cacher.
    for word in voted:
        if not word["deleted"] or not word["disputed"] or not segments:
            continue
        middle = (word["start"] + word["end"]) / 2.0
        target = min(
            segments,
            key=lambda segment: (
                0.0 if segment["startMs"] <= middle <= segment["endMs"] else
                min(abs(middle - segment["startMs"]), abs(middle - segment["endMs"])),
                segment["startMs"],
            ),
        )
        target["variants"].append({
            "startMs": round(word["start"], 1), "endMs": round(word["end"], 1),
            "chosen": "", "alternatives": word["alternatives"],
        })
        target["variants"].sort(key=lambda variant: (variant["startMs"], variant["endMs"], variant["chosen"]))
        target["isDisputed"] = True
        known = {(source["by"], source["rec"]) for source in target["sources"]}
        known.update(word["sources"])
        target["sources"] = [{"by": by, "rec": rec} for by, rec in sorted(known)]
        target["by"] = sorted({by for by, _ in known})
        target["rec"] = sorted({rec for _, rec in known})
        support_keys = {(item["by"], item["rec"], item["segment"], item["word"])
                        for item in target["support"]}
        for item in word["support"]:
            key = (item["by"], item["rec"], item["segment"], item["word"])
            if key not in support_keys:
                support_keys.add(key)
                target["support"].append(item)
    return segments


def fuse_recordings(recordings: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Fusionne des enregistrements et renvoie ``segments``, ``originals`` et ``stats``.

    ``recordings`` accepte le schéma existant (id/by/segments), plus les champs
    facultatifs quality et clock. Les entrées vides ou invalides apparaissent dans
    stats.excluded ; toutes les autres sont alignées sur le meilleur pivot.
    """
    originals = copy.deepcopy(list(recordings or []))
    streams = []
    excluded = []
    for order, recording in enumerate(originals):
        if not isinstance(recording, dict):
            excluded.append("recording-%06d" % order)
            continue
        stream = _flatten(recording, order)
        if stream is None:
            excluded.append(str(recording.get("id") or "recording-%06d" % order))
        else:
            streams.append(stream)

    if not streams:
        return {"segments": [], "originals": originals, "stats": {
            "mode": "empty", "streamCount": len(originals), "aligned": 0,
            "excluded": excluded, "disputed": 0,
        }}

    pivot = min(streams, key=lambda stream: (-stream["quality"], -len(stream["words"]), stream["id"], stream["order"]))
    for stream in streams:
        if stream is pivot:
            continue
        slope, offset, anchors = _affine_to_pivot(stream, pivot)
        stream["anchors"] = anchors
        _apply_affine(stream, slope, offset)
    # Le pivot ouvre le treillis. Les autres flux sont ajoutés dans un ordre
    # qualitatif et lexical stable : permuter la liste d'entrée ne change donc pas
    # le texte final quand les id/by sont les mêmes.
    slots = [{"observations": [_observation(word, pivot)]} for word in pivot["words"]]
    remaining = sorted((stream for stream in streams if stream is not pivot),
                       key=lambda stream: (-stream["quality"], stream["id"], stream["by"], stream["order"]))
    for stream in remaining:
        slots = _add_stream(slots, stream)

    voted = [_vote(slot, streams) for slot in slots]
    segments = _canonical_segments(voted)
    disputed = sum(1 for word in voted if word["disputed"])
    return {
        "segments": segments,
        "originals": originals,
        "stats": {
            "mode": "single" if len(streams) == 1 else "rover",
            "streamCount": len(originals), "aligned": len(streams),
            "excluded": excluded, "disputed": disputed,
        },
    }


__all__ = ["fuse_recordings"]
