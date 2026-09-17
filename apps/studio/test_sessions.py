#!/usr/bin/env python3
"""Fusion des séances : deux étudiants, un seul cours, zéro doublon."""
from __future__ import annotations
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).parent))
from sessions import (blank_session, consensus_result, merge_contribution,  # noqa: E402
                      merged_segments, parse_ics, refresh_consensus,
                      schedule_from_ics, session_key, session_summary)

F: list[str] = []
def check(name, cond):
    print(f"  {'OK    ' if cond else 'ÉCHEC '}{name}")
    if not cond: F.append(name)

# 1. Deux agendas décrivant le même cours → la MÊME clé.
lea  = session_key("Machine Learning", datetime(2026, 9, 16, 14, 0, tzinfo=timezone.utc))
leo  = session_key("Machine Learning", datetime(2026, 9, 16, 14, 3, tzinfo=timezone.utc))  # 3 min d'écart
check("deux heures proches → même clé (arrondi au créneau)", lea == leo)
check("clé lisible", lea == "machine-learning-20260916-1400")
check("cours différent → clé différente",
      session_key("Algèbre", datetime(2026, 9, 16, 14, 0, tzinfo=timezone.utc)) != lea)
check("heure de Paris et UTC → même clé",
      session_key("Machine Learning", datetime(2026, 9, 16, 16, 0,
                                               tzinfo=ZoneInfo("Europe/Paris"))) == lea)

# 2. ICS réel (plié, TZID, journée entière).
ics = """BEGIN:VCALENDAR\r
BEGIN:VEVENT\r
UID:evt-1@ade\r
SUMMARY:Machine Learning\r
DTSTART:20260916T140000Z\r
DTEND:20260916T160000Z\r
LOCATION:Amphi B\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:evt-2@emlyon\r
SUMMARY:Maths\\, appliquées\r
DTSTART:20260916T100000Z\r
DTEND:20260916T120000Z\r
END:VEVENT\r
END:VCALENDAR"""
events = parse_ics(ics)
check("ICS : deux événements lus", len(events) == 2)
check("ICS : intitulé déséchappé", events[1]["course"] == "Maths, appliquées")
book = schedule_from_ics(ics, "centrale")
check("ICS : carnet indexé par clé", "machine-learning-20260916-1400" in book)
check("ICS : lieu conservé", book["machine-learning-20260916-1400"]["location"] == "Amphi B")

# 2b. Formats fréquents des exports : pliage, heure flottante, journée entière,
# et changement d'heure Europe/Paris.
variants = """BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:Cours très long\nDTSTART;TZID=Europe/Paris:20261025T083000\nDTEND;TZID=Europe/Paris:20261025T100000\nLOCATION:Salle\nEND:VEVENT\nBEGIN:VEVENT\nSUMMARY:Journée entière\nDTSTART;VALUE=DATE:20260917\nEND:VEVENT\nBEGIN:VEVENT\nSUMMARY:Un cours plié qui continue \n ici\nDTSTART:20260916T140000Z\nEND:VEVENT\nEND:VCALENDAR"""
variant_events = parse_ics(variants)
check("ICS : TZID et DST conservés", variant_events[0]["start"].isoformat() == "2026-10-25T07:30:00+00:00")
check("ICS : journée entière", variant_events[1]["start"].isoformat() == "2026-09-17T00:00:00+00:00")
check("ICS : ligne pliée", variant_events[2]["course"] == "Un cours plié qui continue ici")

# 3. Deux étudiants contribuent à la même séance — rien ne s'écrase.
s = blank_session(lea, book[lea])
s = merge_contribution(s, "Léo", "recording",
    {"id": "leo-0", "mime": "audio/webm", "url": "/audio/x/0", "startMs": 0, "endMs": 300000,
     "segments": [{"text": "the loss function", "startMs": 1000, "endMs": 3000, "avgConfidence": 0.9}]})
s = merge_contribution(s, "Léa", "recording",
    {"id": "lea-0", "mime": "audio/webm", "url": "/audio/y/0", "startMs": 0, "endMs": 320000,
     "segments": [{"text": "gradient descent", "startMs": 1200, "endMs": 3200, "avgConfidence": 0.95}]})
s = merge_contribution(s, "Léa", "photo",
    {"name": "tableau.jpg", "kind": "image", "text": "J = RSS + lambda ||beta||^2"})
check("deux enregistrements de contributeurs différents coexistent", len(s["recordings"]) == 2)
check("deux contributeurs listés", set(s["contributors"]) == {"Léo", "Léa"})
check("une pièce jointe", len(s["attachments"]) == 1)

# 4. Idempotence : rejouer une contribution ne double rien.
s = merge_contribution(s, "Léo", "recording",
    {"id": "leo-0", "mime": "audio/webm", "url": "/audio/x/0", "startMs": 0, "endMs": 300000, "segments": []})
s = merge_contribution(s, "Léa", "photo", {"name": "tableau.jpg", "kind": "image", "text": "J = RSS + lambda ||beta||^2"})
check("enregistrement rejoué → pas de doublon", len(s["recordings"]) == 2)
check("photo rejouée → pas de doublon", len(s["attachments"]) == 1)

# 5. Union des paroles, ordonnée, attribuée.
segs = merged_segments(s)
# leo-0 a été rejoué avec segments vides → il ne reste que la parole de Léa
check("paroles fusionnées et ordonnées", [x["text"] for x in segs] == ["gradient descent"])
check("chaque parole sait de qui elle vient", segs[0]["by"] == "Léa" and segs[0]["rec"] == "lea-0")

# 6. Résumé pour la liste.
summ = session_summary(s)
check("résumé : bon nombre de contributeurs", len(summ["contributors"]) == 2)
check("résumé : cours issu de l'agenda", summ["course"] == "Machine Learning")
check("résumé : statistiques de consensus", summ["consensus"]["mode"] == "single")

# 7. Cache ROVER additif : stable tant que les audios ne changent pas, invalidé sinon.
s2 = blank_session("test-rover")
for rec_id, contributor, word in (("a", "Ana", "descent"), ("b", "Basile", "descent")):
    s2 = merge_contribution(s2, contributor, "recording", {
        "id": rec_id, "segments": [{"text": "gradient " + word, "startMs": 0,
                                     "endMs": 1000, "avgConfidence": 0.9}],
    })
refresh_consensus(s2)
cached = s2["consensus"]
check("cache consensus relu sans recalcul visible", consensus_result(s2) is cached)
check("deux flux passent par ROVER", cached["stats"]["mode"] == "rover" and
      cached["segments"][0]["text"] == "gradient descent")
s2 = merge_contribution(s2, "Ana", "recording", {
    "id": "a", "segments": [{"text": "gradient ascent", "startMs": 0,
                                "endMs": 1000, "avgConfidence": 0.9}],
})
check("remplacement audio invalide le cache", "consensus" not in s2)
refresh_consensus(s2)
check("empreinte change après remplacement", s2["consensus"]["fingerprint"] != cached["fingerprint"])

if F:
    print("\nÉCHECS :", F); sys.exit(1)
print("\nTous les cas passent.")
