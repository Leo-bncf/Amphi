#!/usr/bin/env python3
"""
Mesure n°2 de M1 — débit de Whisper sur le Mac (ADR-15).

Tout le modèle de coût d'Amphi repose sur l'hypothèse « le M4 transcrit à 10 à 30x
le temps réel ». Si c'est 3x, le worker local reste utile mais le repli payant
devient la norme et le budget remonte. Ce script mesure, il ne suppose pas.

Le facteur temps réel (RTF) exclut délibérément le chargement du modèle : le worker
le garde en mémoire entre les chunks, ce n'est pas un coût récurrent.

Usage:
    .venv/bin/python bench_whisper.py [--model REPO] [--audio FICHIER]
"""

from __future__ import annotations

import argparse
import json
import re
import time
import unicodedata
import wave

import numpy as np
from datetime import datetime, timezone
from pathlib import Path

BENCH_DIR = Path(__file__).parent
DEFAULT_AUDIO = BENCH_DIR / "fixtures" / "cours-regularisation.wav"
DEFAULT_REFERENCE = BENCH_DIR / "fixtures" / "cours-regularisation.txt"
RESULTS_DIR = BENCH_DIR / "results"

SAMPLE_RATE = 16_000

# Les chunks font 20 à 30 s en production : c'est la granularité qui compte,
# pas le fichier d'une heure. On mesure les deux.
CHUNK_SECONDS = 25.0


def load_wav_mono16k(path: Path) -> np.ndarray:
    """
    Charge un WAV PCM 16 bits mono en float32 [-1, 1].

    On décode nous-mêmes plutôt que de laisser mlx_whisper passer par ffmpeg :
    le binaire n'est pas forcément installé, et pour du WAV il n'apporte rien.
    En production, le worker reçoit de l'Ogg/Opus et a bien besoin de ffmpeg —
    fourni là-bas par imageio-ffmpeg, sans Homebrew.
    """
    with wave.open(str(path)) as w:
        if w.getnchannels() != 1 or w.getframerate() != SAMPLE_RATE or w.getsampwidth() != 2:
            raise SystemExit(
                f"{path.name}: attendu PCM 16 bits mono {SAMPLE_RATE} Hz, "
                f"obtenu {w.getsampwidth() * 8} bits / {w.getnchannels()} canaux / {w.getframerate()} Hz"
            )
        frames = w.readframes(w.getnframes())
    return np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0


def normalize(text: str) -> list[str]:
    """Normalisation pour le WER : casse, accents, ponctuation, espaces."""
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return text.split()


def wer(reference: str, hypothesis: str) -> tuple[float, int, int]:
    """Word error rate par distance de Levenshtein sur les tokens normalisés."""
    ref, hyp = normalize(reference), normalize(hypothesis)
    if not ref:
        return 0.0, 0, 0
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i]
        for j, h in enumerate(hyp, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h)))
        prev = cur
    return prev[-1] / len(ref), prev[-1], len(ref)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="mlx-community/whisper-large-v3-turbo")
    parser.add_argument("--audio", type=Path, default=DEFAULT_AUDIO)
    parser.add_argument("--reference", type=Path, default=DEFAULT_REFERENCE)
    args = parser.parse_args()

    if not args.audio.exists():
        raise SystemExit(f"Audio introuvable : {args.audio}\nGénère-le d'abord (voir bench/README.md).")

    import mlx_whisper  # importé tard : l'import seul coûte quelques secondes

    audio = load_wav_mono16k(args.audio)
    duration = audio.size / SAMPLE_RATE
    print(f"Audio    : {args.audio.name} — {duration:.1f} s ({duration / 60:.2f} min)")
    print(f"Modèle   : {args.model}")
    print("Premier passage : chargement du modèle en mémoire.\n")

    # Passe 1 : téléchargement + chargement + transcription. Donne le coût à froid.
    cold_start = time.perf_counter()
    result = mlx_whisper.transcribe(
        audio,
        path_or_hf_repo=args.model,
        word_timestamps=True,
        language=None,  # jamais imposée : les cours mélangent FR et EN
        verbose=False,
    )
    cold_elapsed = time.perf_counter() - cold_start

    # Passe 2 : modèle déjà en cache mémoire. C'est le régime du worker.
    warm_start = time.perf_counter()
    result = mlx_whisper.transcribe(
        audio,
        path_or_hf_repo=args.model,
        word_timestamps=True,
        language=None,
        verbose=False,
    )
    warm_elapsed = time.perf_counter() - warm_start

    text = result.get("text", "").strip()
    segments = result.get("segments", [])
    rtf = duration / warm_elapsed
    per_chunk_s = CHUNK_SECONDS / rtf

    reference = args.reference.read_text(encoding="utf-8") if args.reference.exists() else ""
    wer_value, errors, ref_words = wer(reference, text) if reference else (float("nan"), 0, 0)

    # 130 h de cours par mois, 3 flux transcrits (profil Fusion par défaut).
    monthly_hours = 130.0 * 3
    monthly_compute_h = monthly_hours / rtf

    print(f"À froid           : {cold_elapsed:6.1f} s (chargement du modèle inclus)")
    print(f"À chaud           : {warm_elapsed:6.1f} s")
    print(f"Facteur temps réel: {rtf:6.1f}x")
    print(f"Chunk de {CHUNK_SECONDS:.0f} s      : {per_chunk_s:6.2f} s de calcul")
    print(f"Segments          : {len(segments)}")
    print(f"Langue détectée   : {result.get('language')}")
    if reference:
        print(f"WER vs référence  : {wer_value:6.1%} ({errors} erreurs / {ref_words} mots)")
        print("  (audio de synthèse, donc anormalement propre : c'est un plancher, pas une prévision)")
    print(f"\nÀ 130 h de cours/mois x 3 flux = {monthly_hours:.0f} h d'audio")
    print(f"  soit {monthly_compute_h:.1f} h de calcul par mois sur ce Mac.")
    verdict = "TIENT" if rtf >= 10 else ("JUSTE" if rtf >= 3 else "INSUFFISANT")
    print(f"  Hypothèse de l'ADR-15 (10 à 30x) : {verdict}")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / "whisper-m4.json"
    out.write_text(
        json.dumps(
            {
                "ranAt": datetime.now(timezone.utc).isoformat(),
                "model": args.model,
                "audioFile": args.audio.name,
                "audioDurationS": round(duration, 2),
                "coldElapsedS": round(cold_elapsed, 2),
                "warmElapsedS": round(warm_elapsed, 2),
                "realtimeFactor": round(rtf, 2),
                "secondsPerChunk": round(per_chunk_s, 3),
                "segmentCount": len(segments),
                "detectedLanguage": result.get("language"),
                "wer": None if reference == "" else round(wer_value, 4),
                "monthlyComputeHours": round(monthly_compute_h, 2),
                "verdict": verdict,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    (RESULTS_DIR / "whisper-m4-transcript.txt").write_text(text + "\n", encoding="utf-8")
    print(f"\nRésultats  : {out}")
    print(f"Transcript : {RESULTS_DIR / 'whisper-m4-transcript.txt'}")


if __name__ == "__main__":
    main()
