#!/usr/bin/env python3
"""ROVER : plusieurs micros, une transcription canonique et traçable."""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rover import fuse_recordings  # noqa: E402


FAILURES = []


def check(name, condition):
    print("  %s%s" % ("OK    " if condition else "ÉCHEC ", name))
    if not condition:
        FAILURES.append(name)


def recording(rec_id, by, phrase, start=0.0, step=500.0, quality=0.9,
              substitutions=None, omit=None, insert=None, affine=(1.0, 0.0)):
    """Petit fabricant de flux mot-à-mot ; affine simule l'horloge du téléphone."""
    substitutions = substitutions or {}
    omit = set(omit or [])
    insert = insert or {}
    slope, offset = affine
    words = []
    source = phrase.split()
    for index, original in enumerate(source):
        for extra in insert.get(index, []):
            raw_start = (start + index * step - step * 0.45 - offset) / slope
            words.append({"text": extra, "startMs": raw_start,
                          "endMs": raw_start + step * 0.35 / slope, "confidence": 0.9})
        if index in omit:
            continue
        raw_start = (start + index * step - offset) / slope
        words.append({"text": substitutions.get(index, original), "startMs": raw_start,
                      "endMs": raw_start + step * 0.8 / slope, "confidence": 0.9})
    end = max((word["endMs"] for word in words), default=start)
    return {"id": rec_id, "by": by, "quality": quality,
            "segments": [{"text": " ".join(word["text"] for word in words),
                           "startMs": words[0]["startMs"] if words else start,
                           "endMs": end, "words": words, "avgConfidence": 0.9}]}


def fused_text(result):
    return " ".join(segment["text"] for segment in result["segments"])


PHRASE = "nous calculons maintenant la descente de gradient avec un taux constant"

# 1. Décalage fixe et dérive affine bornée : les ancres remettent les horloges ensemble.
pivot = recording("a", "Alice", PHRASE, start=1_000, step=700, quality=0.95)
offset = recording("b", "Bob", PHRASE, start=1_000, step=700, quality=0.85,
                   affine=(1.0, 4_200.0))
drift = recording("c", "Chloé", PHRASE, start=1_000, step=700, quality=0.8,
                  affine=(1.02, -2_500.0))
clocked = fuse_recordings([pivot, offset, drift])
check("décalage robuste : un seul texte", fused_text(clocked) == PHRASE)
check("dérive affine : les trois flux sont alignés", clocked["stats"]["aligned"] == 3)
check("temps ramenés sur le pivot", clocked["segments"][0]["startMs"] == 1000.0)

# 2. Substitution, insertion et suppression explicites dans le vote.
changed = recording("d", "David", PHRASE, start=1_000, step=700, quality=0.9,
                    substitutions={4: "montée"}, omit={8}, insert={6: ["rapidement"]})
consensus = fuse_recordings([pivot, offset, changed])
check("la majorité gagne la substitution", "descente" in fused_text(consensus) and "montée" not in fused_text(consensus))
check("une insertion minoritaire ne pollue pas le canon", "rapidement" not in fused_text(consensus))
check("une suppression minoritaire ne supprime pas le mot", fused_text(consensus).endswith("taux constant"))
check("insertions/suppressions restent visibles comme variantes", consensus["stats"]["disputed"] >= 2 and
      any(alt["text"] == "rapidement" for segment in consensus["segments"]
          for variant in segment["variants"] for alt in variant["alternatives"]))

# 3. Un contributeur avec trois chunks ne dispose toujours que d'une voix par variante.
wrong_1 = recording("leo-1", "Léo", PHRASE, substitutions={4: "montée"}, quality=0.99)
wrong_2 = recording("leo-2", "Léo", PHRASE, substitutions={4: "montée"}, quality=0.98)
wrong_3 = recording("leo-3", "Léo", PHRASE, substitutions={4: "montée"}, quality=0.97)
right_1 = recording("lea", "Léa", PHRASE, quality=0.8)
right_2 = recording("ines", "Inès", PHRASE, quality=0.8)
capped = fuse_recordings([wrong_1, wrong_2, wrong_3, right_1, right_2])
check("la majorité de personnes bat les chunks d'un même auteur", "descente" in fused_text(capped) and "montée" not in fused_text(capped))

# 4. Désaccord partagé : le texte choisit sans cacher l'incertitude.
dispute_a = recording("x", "X", PHRASE, substitutions={4: "descente"}, quality=0.9)
dispute_b = recording("y", "Y", PHRASE, substitutions={4: "montée"}, quality=0.9)
disputed = fuse_recordings([dispute_a, dispute_b])
variant_words = [variant for segment in disputed["segments"] for variant in segment["variants"]]
check("égalité signalée comme dispute", disputed["stats"]["disputed"] >= 1 and disputed["segments"][0]["isDisputed"])
check("variantes lexicales exposées", any({alt["text"] for alt in variant["alternatives"]} >= {"descente", "montée"} for variant in variant_words))

# 5. Ancien schéma : texte seul, sans mots ni confiance.
legacy = {"id": "ancien", "by": "Archive", "segments": [
    {"text": "bonjour le vieux cours", "startMs": 2_000, "endMs": 4_000}
]}
legacy_result = fuse_recordings([legacy])
check("texte historique interpolé", fused_text(legacy_result) == "bonjour le vieux cours")
check("confiance historique neutre", legacy_result["segments"][0]["avgConfidence"] == 0.5)

# 6. Bord de chunks répété : la phrase commune n'est rendue qu'une fois.
duplicate = {"id": "dup", "by": "Lina", "segments": [
    {"text": "le modèle apprend", "startMs": 0, "endMs": 1_500},
    {"text": "modèle apprend très vite", "startMs": 500, "endMs": 2_500},
]}
deduped = fuse_recordings([duplicate])
check("chevauchement de frontière dédupliqué", fused_text(deduped) == "le modèle apprend très vite")

# 7. Segments canoniques : coupure au silence et à quinze secondes maximum.
long_phrase = " ".join("mot%d" % index for index in range(40))
long = recording("long", "Nora", long_phrase, step=600)
# Un silence de plus de 700 ms au milieu.
for word in long["segments"][0]["words"][25:]:
    word["startMs"] += 1_000
    word["endMs"] += 1_000
long_result = fuse_recordings([long])
check("segments limités à quinze secondes", all(seg["endMs"] - seg["startMs"] <= 15_000 for seg in long_result["segments"]))
check("silence supérieur à 700 ms coupe un segment", len(long_result["segments"]) >= 2)

# 8. Provenance complète, ordre stable, originaux préservés et aucune mutation.
inputs = [copy.deepcopy(offset), copy.deepcopy(pivot), copy.deepcopy(changed)]
before = copy.deepcopy(inputs)
first = fuse_recordings(inputs)
second = fuse_recordings(inputs)
check("sources by/rec conservées", set(first["segments"][0]["by"]) == {"Alice", "Bob", "David"} and
      set(first["segments"][0]["rec"]) == {"a", "b", "d"})
check("provenance structurée", {tuple(sorted(source.items())) for source in first["segments"][0]["sources"]} >=
      {(("by", "Alice"), ("rec", "a")), (("by", "Bob"), ("rec", "b"))})
check("originaux copiés sans perte", first["originals"] == before and first["originals"] is not inputs)
check("aucune mutation des entrées", inputs == before)
check("sortie déterministe", json.dumps(first, sort_keys=True, ensure_ascii=False) ==
      json.dumps(second, sort_keys=True, ensure_ascii=False))

# 9. Flux vide explicitement exclu, statistiques complètes.
with_empty = fuse_recordings([pivot, {"id": "vide", "by": "Silence", "segments": []}])
check("flux vide exclu et compté", with_empty["stats"] == {
    "mode": "single", "streamCount": 2, "aligned": 1, "excluded": ["vide"], "disputed": 0,
})

# 10. Sans ancre commune de deux mots, l'ordre lexical reste un repli exploitable.
no_anchor_a = recording("na", "Ana", "alpha beta", quality=0.9)
no_anchor_b = recording("nb", "Basile", "alpha gamma", quality=0.8)
no_anchor = fuse_recordings([no_anchor_a, no_anchor_b])
check("repli séquentiel sans ancre", no_anchor["stats"]["aligned"] == 2 and
      no_anchor["segments"][0]["isDisputed"])

# 11. Le modèle d'horloge partagé {a,b} corrige bien les temps locaux.
clocked_single = recording("clock", "Camille", "un deux trois", start=1_000)
clocked_single["clock"] = {"a": 1.02, "b": 400.0}
clock_result = fuse_recordings([clocked_single])
check("horloge officielle a/b", clock_result["segments"][0]["startMs"] == 1420.0)

# 12. La qualité structurée choisit le meilleur pivot et pèse le vote.
clean = recording("clean", "Dina", "le résultat est exact", quality=0.1,
                  substitutions={3: "exact"})
clean["quality"] = {"snr": 30, "meanConfidence": 0.95, "gapRatio": 0.0}
noisy = recording("noise", "Eli", "le résultat est faux", quality=0.99,
                  substitutions={3: "faux"})
noisy["quality"] = {"snr": 0, "meanConfidence": 0.3, "gapRatio": 0.8}
quality_result = fuse_recordings([clean, noisy])
check("qualité SNR/confiance/trous pondérée", "exact" in fused_text(quality_result))

# 13. Une longue transcription ne construit pas une matrice quadratique globale.
long_text = " ".join("token%d" % index for index in range(3_000))
long_a = recording("long-a", "Farah", long_text, step=80)
long_b = recording("long-b", "Gabin", long_text, step=80, affine=(1.002, 700.0))
long_fused = fuse_recordings([long_a, long_b])
check("long cours fusionné sans perte", len(" ".join(seg["text"] for seg in long_fused["segments"]).split()) == 3_000)
check("provenance détaillée", all({"segment", "word", "startMs", "endMs"} <= set(item)
                                  for item in long_fused["segments"][0]["support"]))

if FAILURES:
    print("\nÉCHECS :", FAILURES)
    sys.exit(1)
print("\nTous les cas ROVER passent.")
