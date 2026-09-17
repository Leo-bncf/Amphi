#!/usr/bin/env python3
"""
Garde-fou sur les diagrammes — Mermaid ne tourne que dans le navigateur.

Les cas valides ci-dessous ont été vérifiés par le vrai parseur Mermaid 11.17.2
(celui qui est vendorisé dans l'app), et les cas cassés viennent de vraies
séances. `mermaid_problems` ne remplace pas le parseur : il attrape ce dont on
a la preuve que ça casse, avant de facturer un aller-retour au modèle.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from server import mermaid_problems  # noqa: E402

FAILURES: list[str] = []

# Vérifiés OK par Mermaid 11.17.2 — le vérificateur ne doit rien y trouver.
VALIDES = {
    "ponctuation et unicode protégés par les guillemets":
        'flowchart TD\n  A["Function f: E → F"] --> B["Injective (one-to-one)"]\n'
        '  E["Set operations"] --> H["Difference (A \\ B)"]\n'
        '  I["Sequence A_n"] --> J["Increasing: A_n ⊆ A_{n+1}"]',
    "libellés d'arêtes":
        'flowchart TD\n  A["Registers"] -->|Fastest access| B["L1 Cache"]\n'
        '  B -->|Slower| C["RAM"]',
    "accolades et barres verticales dans un libellé":
        'flowchart TD\n  C["Find x"] --> D["Preimage: f⁻¹({y}) = {x₁, x₂}"]\n'
        '  D --> E["If |f⁻¹({y})| = 1, it is defined"]',
    "boucle sur soi-même":
        'flowchart TD\n  Server["Server"] -->|Renders response| Server',
}

# Cassés pour de bon : le parseur les refuse, le vérificateur doit les voir.
CASSES = {
    "classDiagram — le deux-points est de la grammaire, pas du texte":
        "classDiagram\n  class BusSystems {\n    +Address bus: Specifies memory locations\n"
        "    +Data bus: Transfers data\n  }",
    "classDiagram — l'accolade ferme le bloc au milieu d'un membre":
        "classDiagram\n  class Sequence {\n    +increasingSequence(A_n) A_n ⊆ A_{n+1}\n  }",
    "guillemet non fermé": 'flowchart TD\n  A["Bus de données] --> B["RAM"]',
    "crochets non appariés": 'flowchart TD\n  A["RAM" --> B["CPU"]',
    "diagramme vide": "   \n  ",
}

for nom, code in VALIDES.items():
    found = mermaid_problems(code)
    if found:
        FAILURES.append(f"faux positif — {nom} : {found}")
    print(f"  {'OK    ' if not found else 'RATÉ  '}{nom}")

for nom, code in CASSES.items():
    if not mermaid_problems(code):
        FAILURES.append(f"non détecté — {nom}")
    print(f"  {'REFUSÉ' if mermaid_problems(code) else 'RATÉ  '} {nom}")

if FAILURES:
    print("\nÉCHECS :")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print("\nTous les cas passent.")
