#!/usr/bin/env python3
"""
Serveur de transcription local — ADR-15.

Charge le modèle Whisper une fois et le garde en mémoire : c'est ce qui rend le
worker viable. Recharger le modèle à chaque chunk coûterait plus cher que le
transcrire. Le worker Node (index.ts) tire les jobs de la file et vient taper ici.

Volontairement en bibliothèque standard (plus mlx-whisper et imageio-ffmpeg) :
pas de framework web, pas de Homebrew, rien à compiler. Le worker doit pouvoir
s'installer sur le portable de n'importe quel étudiant de la promo.

    python -m venv .venv
    ./.venv/bin/pip install mlx-whisper imageio-ffmpeg
    ./.venv/bin/python asr_server.py --port 8765
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import numpy as np

LOG = logging.getLogger("amphi.asr")

SAMPLE_RATE = 16_000
DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"

# En dessous, on se déclare indisponible et la file bascule sur le repli payant :
# vider la batterie d'un étudiant en plein cours coûte plus cher que 0,037 €/h.
MIN_BATTERY_PERCENT = 25

_model_lock = threading.Lock()


class Transcriber:
    """Encapsule le modèle. Un seul appel MLX à la fois — le GPU n'est pas partagé."""

    def __init__(self, model_repo: str) -> None:
        self.model_repo = model_repo
        self._warmed = False

    def warm_up(self) -> float:
        """Charge le modèle avec un échantillon de silence. Retourne le temps de chargement."""
        import mlx_whisper

        start = time.perf_counter()
        silence = np.zeros(SAMPLE_RATE, dtype=np.float32)
        with _model_lock:
            mlx_whisper.transcribe(silence, path_or_hf_repo=self.model_repo, verbose=False)
        self._warmed = True
        elapsed = time.perf_counter() - start
        LOG.info("modèle chargé en %.1f s (%s)", elapsed, self.model_repo)
        return elapsed

    @property
    def is_warm(self) -> bool:
        return self._warmed

    def transcribe(
        self, audio: np.ndarray, lexicon: list[str] | None, previous_text: str | None,
        language: str | None = None,
    ) -> dict[str, Any]:
        import mlx_whisper

        # Biasing : on injecte le jargon du cours dans le prompt initial. C'est ce
        # qui fait la différence entre « gradient boosting » et « gradient bousting ».
        prompt_parts: list[str] = []
        if previous_text:
            prompt_parts.append(previous_text.strip()[-400:])
        if lexicon:
            prompt_parts.append("Termes du cours : " + ", ".join(lexicon[:60]) + ".")
        initial_prompt = " ".join(prompt_parts) or None

        start = time.perf_counter()
        with _model_lock:
            result = mlx_whisper.transcribe(
                audio,
                path_or_hf_repo=self.model_repo,
                word_timestamps=True,
                # Imposée quand l'appelant la connaît : en enregistrement par
                # parties, la détection automatique juge chaque tranche
                # séparément et peut basculer en cours de cours.
                language=language,
                initial_prompt=initial_prompt,
                # Sur un micro lointain, Whisper part en boucle — « shocking
                # shocking shocking… » — parce qu'il se conditionne sur son
                # propre texte précédent. Couper ce report casse la boucle.
                condition_on_previous_text=False,
                # Seuils de rejet : en dessous, le segment est du bruit décodé
                # comme de la parole. Mesuré sur un vrai cours d'amphi, ça
                # supprime 26 % des segments pour seulement 15 % des mots.
                no_speech_threshold=0.6,
                logprob_threshold=-1.0,
                compression_ratio_threshold=2.4,
                verbose=False,
            )
        return {"result": result, "processingMs": (time.perf_counter() - start) * 1000}


def ffmpeg_path() -> str:
    """ffmpeg fourni par imageio-ffmpeg : binaire statique, aucun Homebrew requis."""
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def decode_to_pcm(data: bytes, suffix: str) -> np.ndarray:
    """Décode n'importe quel conteneur vers du float32 mono 16 kHz."""
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as tmp:
        tmp.write(data)
        tmp.flush()
        process = subprocess.run(
            [
                ffmpeg_path(), "-nostdin", "-threads", "0",
                "-i", tmp.name,
                "-f", "f32le", "-ac", "1", "-acodec", "pcm_f32le",
                "-ar", str(SAMPLE_RATE), "-",
            ],
            capture_output=True,
            check=False,
        )
    if process.returncode != 0:
        tail = process.stderr.decode("utf-8", "replace")[-400:]
        raise ValueError(f"décodage audio impossible : {tail}")
    return np.frombuffer(process.stdout, dtype=np.float32).copy()


def suffix_for(mime: str) -> str:
    if "ogg" in mime or "opus" in mime:
        return ".ogg"
    if "webm" in mime:
        return ".webm"
    if "mp4" in mime or "m4a" in mime or "aac" in mime:
        return ".m4a"
    if "wav" in mime:
        return ".wav"
    return ".bin"


def battery_status() -> tuple[bool, int | None]:
    """(sur secteur, pourcentage). macOS uniquement ; ailleurs on suppose le secteur."""
    if sys.platform != "darwin":
        return True, None
    try:
        out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return True, None
    on_ac = "AC Power" in out
    match = re.search(r"(\d+)%", out)
    return on_ac, int(match.group(1)) if match else None


def to_asr_result(raw: dict[str, Any], processing_ms: float, model: str, duration_s: float) -> dict[str, Any]:
    """Traduit la sortie de Whisper vers le contrat AsrResult de packages/shared."""
    segments: list[dict[str, Any]] = []
    previous_text = None
    for seg in raw.get("segments", []):
        # Whisper répète parfois la même phrase des dizaines de fois sur du
        # silence. Deux segments consécutifs identiques : le second est un
        # artefact, pas une répétition de l'enseignant.
        current = seg.get("text", "").strip()
        if current and current == previous_text:
            continue
        previous_text = current
        words = [
            {
                "text": w.get("word", "").strip(),
                "startMs": round(float(w.get("start", 0.0)) * 1000, 1),
                "endMs": round(float(w.get("end", 0.0)) * 1000, 1),
                # Whisper donne une probabilité par mot ; on la borne pour ne jamais
                # renvoyer une confiance de 1 qui écraserait le vote de consensus.
                "confidence": min(0.99, max(0.0, float(w.get("probability", 0.5)))),
            }
            for w in seg.get("words", [])
            if w.get("word", "").strip()
        ]
        confidences = [w["confidence"] for w in words]
        segments.append(
            {
                "startMs": round(float(seg.get("start", 0.0)) * 1000, 1),
                "endMs": round(float(seg.get("end", 0.0)) * 1000, 1),
                "text": seg.get("text", "").strip(),
                "words": words,
                "lang": raw.get("language"),
                "avgConfidence": round(sum(confidences) / len(confidences), 4) if confidences else 0.5,
            }
        )
    return {
        "segments": segments,
        "audioDurationMs": round(duration_s * 1000, 1),
        "provider": "mlx-local",
        "model": model,
        "costMilliCents": 0,  # c'est tout l'intérêt
        "processingMs": round(processing_ms, 1),
    }


class Handler(BaseHTTPRequestHandler):
    transcriber: Transcriber

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.debug(fmt, *args)

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/health":
            self._send(404, {"error": "not found"})
            return
        on_ac, percent = battery_status()
        available = self.transcriber.is_warm and (on_ac or percent is None or percent >= MIN_BATTERY_PERCENT)
        self._send(
            200,
            {
                "ok": True,
                "available": available,
                "model": self.transcriber.model_repo,
                "warm": self.transcriber.is_warm,
                "onAcPower": on_ac,
                "batteryPercent": percent,
                "minBatteryPercent": MIN_BATTERY_PERCENT,
            },
        )

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/transcribe":
            self._send(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._send(400, {"error": "corps vide"})
            return

        data = self.rfile.read(length)
        mime = self.headers.get("X-Mime-Type", "audio/ogg")
        lexicon_header = self.headers.get("X-Lexicon")
        previous = self.headers.get("X-Previous-Text")

        try:
            lexicon = json.loads(lexicon_header) if lexicon_header else None
            if lexicon is not None and not isinstance(lexicon, list):
                raise ValueError("X-Lexicon doit être un tableau JSON de chaînes")
            audio = decode_to_pcm(data, suffix_for(mime))
            if audio.size == 0:
                raise ValueError("audio vide après décodage")
            language = self.headers.get("X-Language") or None
            if language in ("", "auto"):
                language = None
            outcome = self.transcriber.transcribe(audio, lexicon, previous, language)
        except ValueError as exc:
            self._send(400, {"error": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001 — le worker doit voir la cause, pas un 502 muet
            LOG.exception("échec de transcription")
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})
            return

        self._send(
            200,
            to_asr_result(
                outcome["result"],
                outcome["processingMs"],
                self.transcriber.model_repo,
                audio.size / SAMPLE_RATE,
            ),
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )

    Handler.transcriber = Transcriber(args.model)
    LOG.info("chargement du modèle — le premier lancement télécharge les poids")
    Handler.transcriber.warm_up()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    LOG.info("prêt sur http://%s:%d", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("arrêt")
        server.shutdown()


if __name__ == "__main__":
    sys.exit(main())
