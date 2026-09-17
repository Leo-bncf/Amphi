#!/usr/bin/env python3
"""
Tests de la vérification d'ancrage — §6.3.

C'est le mécanisme qui distingue des notes qu'on peut réviser d'un résumé qu'il
faut croire sur parole. Une vérification qui n'écarte jamais rien est
indiscernable d'une absence de vérification : ces cas existent pour qu'un
assouplissement futur du seuil se voie tout de suite.

    ./bench/.venv/bin/python apps/studio/test_anchors.py
"""

from __future__ import annotations

import sys
from pathlib import Path

STUDIO = Path(__file__).parent
sys.path.insert(0, str(STUDIO))
sys.path.insert(0, str(STUDIO.parent / "mac-worker"))

from server import speech_indices, verify_anchor  # noqa: E402

SEGMENTS = [
    {"text": "Passons maintenant au lasso, la régularisation L1.", "startMs": 1000, "endMs": 4000},
    {"text": "Le lasso met exactement certains coefficients à zéro,", "startMs": 4000, "endMs": 7000},
    {"text": "alors que le ridge les rapproche de zéro sans jamais les y amener.", "startMs": 7000, "endMs": 11000},
    {"text": "Ça veut dire que le lasso fait de la sélection de variables automatiquement.", "startMs": 11000, "endMs": 15000},
]

ATTACHMENTS = [
    {"name": "tableau.png", "text": "Ridge L2 : SSR + lambda * somme des beta au carre. Standardiser AVANT."},
]

FAILURES: list[str] = []


def check(label: str, block: dict[str, object], expect_kept: bool, expect_kind: str | None = None) -> None:
    anchor = verify_anchor(block, SEGMENTS, ATTACHMENTS)
    kept = anchor is not None
    if kept != expect_kept:
        FAILURES.append(f"{label} — attendu {'gardé' if expect_kept else 'écarté'}, obtenu le contraire")
    if kept and expect_kind and anchor["kind"] != expect_kind:
        FAILURES.append(f"{label} — nature d'ancre {anchor['kind']}, attendu {expect_kind}")
    kind = f" ({anchor['kind']})" if kept else ""
    print(f"  {'GARDÉ ' if kept else 'ÉCARTÉ'}  {label}{kind}")


print("Vérification d'ancrage :\n")

check(
    "reformulation fidèle du segment cité",
    {"type": "paragraph", "text": "Le lasso met certains coefficients exactement à zéro.", "sourceSegmentIds": ["s1"]},
    expect_kept=True,
)
check(
    "plusieurs segments corrects, bornes fusionnées",
    {"type": "paragraph", "text": "Le lasso fait de la sélection de variables.", "sourceSegmentIds": ["s0", "s3"]},
    expect_kept=True,
)
check(
    "titre court, aucun mot plein en commun — légitime",
    {"type": "heading", "level": 2, "text": "Conclusion", "sourceSegmentIds": ["s3"]},
    expect_kept=True,
)
check(
    "hallucination : contenu absent du cours, mais ancre valide",
    {"type": "paragraph", "text": "Napoléon instaura le système métrique pendant la campagne égyptienne.", "sourceSegmentIds": ["s1"]},
    expect_kept=False,
)
check(
    "ancre inventée, hors bornes",
    {"type": "paragraph", "text": "Le lasso annule certains coefficients du modèle.", "sourceSegmentIds": ["s9999"]},
    expect_kept=False,
)
check(
    "aucune ancre",
    {"type": "paragraph", "text": "Une affirmation sans la moindre source citée.", "sourceSegmentIds": []},
    expect_kept=False,
)
check(
    "identifiant malformé",
    {"type": "paragraph", "text": "Le lasso annule certains coefficients du modèle.", "sourceSegmentIds": ["segment-1"]},
    expect_kept=False,
)

check(
    "formule : exemptée du recouvrement lexical, symboles ≠ mots prononcés",
    {"type": "formula", "latex": r"\\beta_j^2", "caption": "pénalité L2", "sourceSegmentIds": ["s1"]},
    expect_kept=True, expect_kind="transcript",
)
check(
    "formule sans source valide : écartée quand même",
    {"type": "formula", "latex": r"\\beta_j^2", "caption": "pénalité", "sourceSegmentIds": ["s404"]},
    expect_kept=False,
)

print("\nSources mixtes — transcription et photo du tableau :\n")

check(
    "bloc tiré de la seule photo, sans horodatage",
    {"type": "paragraph", "text": "Il faut standardiser les variables avant d'appliquer la pénalité.",
     "sourceAttachmentIds": ["a0"]},
    expect_kept=True, expect_kind="attachment",
)
check(
    "bloc citant l'oral ET le tableau — garde l'horodatage cliquable",
    {"type": "paragraph", "text": "Le lasso met certains coefficients à zéro.",
     "sourceSegmentIds": ["s1"], "sourceAttachmentIds": ["a0"]},
    expect_kept=True, expect_kind="transcript",
)
check(
    "hallucination citant une photo authentique",
    {"type": "paragraph", "text": "Napoléon instaura le système métrique pendant la campagne égyptienne.",
     "sourceAttachmentIds": ["a0"]},
    expect_kept=False,
)
check(
    "pièce jointe inexistante",
    {"type": "paragraph", "text": "Il faut standardiser les variables avant la pénalité.",
     "sourceAttachmentIds": ["a42"]},
    expect_kept=False,
)

# Régression : le dictionnaire de détection de langue s'était appelé STOPWORDS
# lui aussi et écrasait celui-ci. content_words cessait alors de filtrer les
# mots creux, et deux textes sans rapport se « recouvraient » sur « cette »,
# « dans », « avec ». La vérification d'ancrage perdait tout son sens.
from server import STOPWORDS, content_words, locate_in_attachments  # noqa: E402

if not isinstance(STOPWORDS, set):
    FAILURES.append(f"STOPWORDS doit rester un ensemble, pas {type(STOPWORDS).__name__}")
creux = content_words("Cette notion est plus dans le cours avec des exemples")
if creux != {"notion", "cours", "exemples"}:
    FAILURES.append(f"mots creux mal filtrés : {sorted(creux)}")

check(
    "recouvrement uniquement sur des mots creux",
    {"type": "paragraph",
     "text": "Cette approche est plus dans cette logique avec ces contraintes budgetaires.",
     "sourceSegmentIds": ["s0"]},
    expect_kept=False,
)

# Un bloc tiré d'une photo doit retrouver sa pièce jointe tout seul : en
# génération fenêtrée le modèle ne cite presque jamais ses sources.
PHOTO = [{"name": "tableau.jpg", "kind": "image",
          "text": "Gradient boosting update rule, nu is the shrinkage parameter"}]
if locate_in_attachments({"type": "paragraph",
                          "text": "The shrinkage parameter scales each tree contribution."}, PHOTO) != ["a0"]:
    FAILURES.append("bloc issu d'une photo non rattaché à sa pièce jointe")
if locate_in_attachments({"type": "paragraph",
                          "text": "Le chat dort sur le canape du salon."}, PHOTO):
    FAILURES.append("bloc hors sujet rattaché à tort à une pièce jointe")

# Une transcription d'une ancienne version ou importée peut ne pas porter de
# score. L'absence de mesure ne doit pas être interprétée comme une confiance
# nulle : sinon la génération partagée ignore silencieusement tout cet audio.
if speech_indices([{"text": "Ridge regression reduces variance by shrinking coefficients."}]) != [0]:
    FAILURES.append("segment sans avgConfidence écarté comme s'il avait une confiance nulle")
if speech_indices([{"text": "bruit indistinct", "avgConfidence": 0.05}]):
    FAILURES.append("segment explicitement peu fiable conservé")

# Les bornes doivent couvrir tous les segments cités : c'est ce qui permet au
# clic sur l'ancre de tomber au bon endroit dans l'audio.
anchor = verify_anchor(
    {"type": "paragraph", "text": "Le lasso fait de la sélection de variables.", "sourceSegmentIds": ["s0", "s3"]},
    SEGMENTS,
)
assert anchor is not None
if anchor["startMs"] != 1000 or anchor["endMs"] != 15000:
    FAILURES.append(f"bornes incorrectes : {anchor}")
print(f"\n  bornes fusionnées : {anchor['startMs']} → {anchor['endMs']} ms")

if FAILURES:
    print("\nÉCHECS :")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print("\nTous les cas passent.")
