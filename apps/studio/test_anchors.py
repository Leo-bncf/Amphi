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

from server import verify_anchor  # noqa: E402

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
