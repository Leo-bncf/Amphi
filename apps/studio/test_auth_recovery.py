#!/usr/bin/env python3
"""Standalone auth, authorization, and recovery safety checks (no pytest)."""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).parent
os.environ["AMPHI_AUTH_DIR"] = tempfile.mkdtemp(prefix="amphi-auth-")
import sys
sys.path.insert(0, str(ROOT))
import auth  # noqa: E402
import recovery  # noqa: E402
import server  # noqa: E402

FAILURES = []
def check(name, condition):
    print(f"  {'OK    ' if condition else 'ÉCHEC '}{name}")
    if not condition: FAILURES.append(name)

def main():
    invited = auth.admin_invite("alice", "Alice", "contributor")
    check("invitation crée un utilisateur", invited["user"]["role"] == "contributor")
    logged = auth.login("alice", invited["password"])
    check("connexion émet un jeton", logged is not None)
    token = logged["token"]
    check("jeton authentifié", auth.authenticate(token)["id"] == invited["user"]["id"])
    auth.logout(token)
    check("déconnexion révoque le jeton", auth.authenticate(token) is None)
    logged = auth.login("alice", invited["password"])
    token = logged["token"]
    tokens = auth._load(auth._tokens_path())
    key = next(iter(tokens))
    tokens[key]["expires"] = time.time() - 1
    auth._save(auth._tokens_path(), tokens)
    check("jeton expiré refusé", auth.authenticate(logged["token"]) is None)
    rotated = auth.admin_rotate(invited["user"]["id"])
    check("rotation change le mot de passe", auth.login("alice", invited["password"]) is None)
    check("nouveau mot de passe fonctionne", auth.login("alice", rotated["password"]) is not None)
    revoked = auth.admin_revoke(invited["user"]["id"])
    check("révocation admin supprime les jetons", revoked >= 1)
    active = auth.login("alice", rotated["password"])
    auth.admin_set_disabled(invited["user"]["id"], True)
    check("utilisateur désactivé refusé", auth.authenticate(active["token"]) is None)

    class Handler:
        headers = {"Authorization": "Bearer token"}
        def _identity(self): return {"id": "owner"}
    h = Handler()
    check("ressource propriétaire autorisée", server.StudioHandler._resource_allowed(h, "owner"))
    check("ressource étrangère refusée", not server.StudioHandler._resource_allowed(h, "other"))
    check("ressource sans propriétaire autorisée", server.StudioHandler._resource_allowed(h, None))

    with tempfile.TemporaryDirectory(prefix="amphi-recovery-") as td:
        root, backups, output = (Path(td) / x for x in ("data", "backups", "restored"))
        root.mkdir(); (root / "sessions.json").write_text('{"ok": true}', encoding="utf-8")
        made = recovery.backup(root, backups)
        manifest = json.loads((made / "manifest.json").read_text())
        check("manifest contient les empreintes", manifest["files"]["sessions.json"])
        recovery.restore(made, output)
        check("restauration non destructive", (output / "sessions.json").read_text() == '{"ok": true}')
        tampered = backups / "tampered"; recovery.shutil.copytree(made, tampered)
        (tampered / "data" / "sessions.json").write_text("tampered", encoding="utf-8")
        try: recovery.restore(tampered, Path(td) / "bad")
        except ValueError: check("restauration refuse données altérées", True)
        else: check("restauration refuse données altérées", False)
    if FAILURES:
        print("\nÉCHECS :", FAILURES); return 1
    print("\nTous les cas auth/recovery passent."); return 0

if __name__ == "__main__": raise SystemExit(main())
