#!/usr/bin/env python3
"""HTTP partagé : contribution atomique, canon ROVER et relecture persistée."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).parent
FAILURES: list[str] = []


def check(name: str, condition: bool) -> None:
    print("  %s%s" % ("OK    " if condition else "ÉCHEC ", name))
    if not condition:
        FAILURES.append(name)


def request(base: str, path: str, payload: dict | None = None,
            headers: dict[str, str] | None = None) -> dict:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request_headers = {"Content-Type": "application/json"} if body else {}
    request_headers.update(headers or {})
    req = Request(base + path, data=body, headers=request_headers,
                  method="POST" if body else "GET")
    with urlopen(req, timeout=5) as response:
        return json.load(response)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


with tempfile.TemporaryDirectory(prefix="amphi-http-") as folder:
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "AMPHI_DATA_DIR": folder, "AMPHI_HOST": "127.0.0.1",
           "PORT": str(port), "AMPHI_PASSWORD": ""}
    # On instancie le serveur HTTP sans main() : le Mac de développement possède
    # le module ASR mais pas forcément ses poids/dépendances. Ces routes ne doivent
    # jamais charger Whisper, comme sur le Tinker Board de production.
    bootstrap = (
        "from http.server import ThreadingHTTPServer; import server; "
        f"ThreadingHTTPServer(('127.0.0.1',{port}),server.StudioHandler).serve_forever()"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", bootstrap], env={**env, "PYTHONPATH": str(ROOT)},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(80):
            try:
                request(base, "/docs")
                break
            except (URLError, ConnectionError):
                time.sleep(0.05)
        else:
            raise RuntimeError("serveur de test indisponible")

        compatibility = request(base, "/compatibility?clientProtocol=1")
        check("compatibilité client/serveur annoncée", compatibility["compatible"] is True
              and compatibility["protocolVersion"] == 1
              and compatibility["serverVersion"])
        stale = request(base, "/compatibility?clientProtocol=0")
        check("client obsolète signalé sans casser health", stale["compatible"] is False
              and request(base, "/health")["ok"] is True)

        def contribute(rec_id: str, by: str, word: str) -> dict:
            return request(base, "/contribute", {
                "sessionKey": "machine-learning-20260916-1400",
                "contributor": by,
                "schedule": {"course": "Machine Learning",
                             "start": "2026-09-16T14:00:00+00:00"},
                "kind": "recording",
                "payload": {
                    "id": rec_id,
                    "startMs": 0,
                    "endMs": 1_000,
                    "segments": [{"text": "gradient " + word, "startMs": 0,
                                  "endMs": 1_000, "avgConfidence": 0.9}],
                },
            })

        first = contribute("ana-0", "Ana", "descent")
        check("première contribution canonique", first["session"]["consensus"]["stats"]["mode"] == "single")
        second = contribute("basile-0", "Basile", "descent")
        consensus = second["session"]["consensus"]
        check("deuxième contribution fusionnée par ROVER", consensus["stats"]["mode"] == "rover")
        check("réponse contient le canon sans transcriptions brutes",
              consensus["segments"][0]["text"] == "gradient descent" and
              "recordings" not in second["session"])

        loaded = request(base, "/load?id=machine-learning-20260916-1400")
        check("cache ROVER persisté", loaded["consensus"]["fingerprint"] == consensus["fingerprint"])
        check("originaux préservés", len(loaded["recordings"]) == 2)
        listed = request(base, "/sessions")
        check("liste expose les statistiques compactes",
              listed["sessions"][0]["consensus"]["mode"] == "rover")
        replay_payload = {
            "sessionKey": "machine-learning-20260916-1400", "contributor": "Basile",
            "kind": "recording", "payload": {"id": "replay-only", "startMs": 2_000,
            "endMs": 3_000, "segments": [{"text": "should appear once", "startMs": 2_000,
            "endMs": 3_000}]}}
        replay_a = request(base, "/contribute", replay_payload,
                           {"Idempotency-Key": "test-receipt-1"})
        replay_b = request(base, "/contribute", replay_payload,
                           {"Idempotency-Key": "test-receipt-1"})
        check("idempotency replay acknowledged without duplicate",
              replay_b.get("duplicate") is True and
              len(replay_b["session"].get("recordings", [])) ==
              len(replay_a["session"].get("recordings", [])))
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)

if FAILURES:
    print("\nÉCHECS :", FAILURES)
    sys.exit(1)
print("\nTous les cas HTTP passent.")
