"""Small file-backed identity and bearer-token store for the studio server."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path

_LOCK = threading.RLock()
_TOKEN_TTL = 30 * 24 * 3600

def _root() -> Path:
    return Path(os.environ.get("AMPHI_AUTH_DIR", os.environ.get("AMPHI_DATA_DIR", "/var/lib/amphi"))) / "auth"

def _users_path() -> Path: return _root() / "users.json"
def _tokens_path() -> Path: return _root() / "tokens.json"
def _invites_path() -> Path: return _root() / "invites.json"

def _load(path: Path) -> dict:
    try: return json.loads(path.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError): return {}

def _save(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)

def _hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    if hasattr(hashlib, "scrypt"):
        digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
        return f"scrypt${salt.hex()}${digest.hex()}"
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000)
    return f"pbkdf2${salt.hex()}${digest.hex()}"

def _check_password(password: str, encoded: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = encoded.split("$", 2)
        salt = bytes.fromhex(salt_hex)
        if scheme == "scrypt" and hasattr(hashlib, "scrypt"):
            actual = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1).hex()
        elif scheme == "pbkdf2":
            actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000).hex()
        else:
            return False
        return hmac.compare_digest(actual, digest_hex)
    except (ValueError, TypeError): return False

def login(username: str, password: str) -> dict | None:
    username = username.strip().lower()
    with _LOCK:
        user = _load(_users_path()).get(username)
        if not isinstance(user, dict) or user.get("disabled") or not _check_password(password, user.get("password", "")):
            return None
        raw = secrets.token_urlsafe(32)
        tokens = _load(_tokens_path())
        tokens[hashlib.sha256(raw.encode()).hexdigest()] = {"user": user["id"], "expires": time.time() + _TOKEN_TTL}
        _save(_tokens_path(), tokens)
        return {"token": raw, "expiresIn": _TOKEN_TTL, "user": public_user(user)}

def authenticate(token: str) -> dict | None:
    with _LOCK:
        users = _load(_users_path())
        tokens = _load(_tokens_path())
        item = tokens.get(hashlib.sha256(token.encode()).hexdigest())
        if not isinstance(item, dict) or float(item.get("expires", 0)) <= time.time(): return None
        for user in users.values():
            if user.get("id") == item.get("user") and not user.get("disabled"): return public_user(user)
        return None

def logout(token: str) -> None:
    with _LOCK:
        tokens = _load(_tokens_path()); tokens.pop(hashlib.sha256(token.encode()).hexdigest(), None); _save(_tokens_path(), tokens)

def _find(users: dict, user_id: str) -> tuple[str, dict] | None:
    for username, user in users.items():
        if isinstance(user, dict) and user.get("id") == user_id:
            return username, user
    return None

def admin_invite(username: str, display_name: str = "", role: str = "contributor") -> dict:
    username = username.strip()
    if not username or role not in {"contributor", "admin"}: raise ValueError("invitation invalide")
    with _LOCK:
        users = _load(_users_path())
        if username in users: raise ValueError("utilisateur déjà existant")
        password = secrets.token_urlsafe(24)
        users[username] = {"id": secrets.token_hex(16), "displayName": display_name or username,
                           "role": role, "disabled": False, "password": _hash_password(password)}
        _save(_users_path(), users)
        code = secrets.token_urlsafe(32)
        invites = _load(_invites_path())
        invites[hashlib.sha256(code.encode()).hexdigest()] = {"username": username, "created": time.time(), "used": False}
        _save(_invites_path(), invites)
        # password remains for backwards-compatible desktop/admin bootstrap; new students use code signup.
        return {"username": username, "password": password, "inviteCode": code, "user": public_user(users[username])}

def signup(username: str, password: str, display_name: str = "") -> dict | None:
    """Create a student account and issue its first bearer token."""
    username = username.strip().lower()
    display_name = display_name.strip()[:120] or username
    if not username or len(username) > 80 or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789._-" for c in username):
        return None
    if not isinstance(password, str) or len(password) < 10:
        return None
    with _LOCK:
        users = _load(_users_path())
        if username in users:
            return None
        user = {"id": secrets.token_hex(16), "displayName": display_name,
                "role": "contributor", "disabled": False, "password": _hash_password(password)}
        users[username] = user
        _save(_users_path(), users)
        raw = secrets.token_urlsafe(32); tokens = _load(_tokens_path())
        tokens[hashlib.sha256(raw.encode()).hexdigest()] = {"user": user["id"], "expires": time.time() + _TOKEN_TTL}
        _save(_tokens_path(), tokens)
        return {"token": raw, "expiresIn": _TOKEN_TTL, "user": public_user(user)}

def admin_set_disabled(user_id: str, disabled: bool) -> dict:
    with _LOCK:
        users = _load(_users_path()); found = _find(users, user_id)
        if not found: raise KeyError("utilisateur introuvable")
        _, user = found; user["disabled"] = bool(disabled)
        _save(_users_path(), users)
        if disabled:
            tokens = _load(_tokens_path())
            tokens = {k: v for k, v in tokens.items() if v.get("user") != user_id}
            _save(_tokens_path(), tokens)
        return public_user(user)

def admin_rotate(user_id: str) -> dict:
    with _LOCK:
        users = _load(_users_path()); found = _find(users, user_id)
        if not found: raise KeyError("utilisateur introuvable")
        _, user = found; password = secrets.token_urlsafe(24)
        user["password"] = _hash_password(password); user["disabled"] = False
        _save(_users_path(), users)
        tokens = _load(_tokens_path()); tokens = {k:v for k,v in tokens.items() if v.get("user") != user_id}; _save(_tokens_path(), tokens)
        return {"password": password, "user": public_user(user)}

def list_users() -> list[dict]:
    with _LOCK:
        return [public_user(user) | {"disabled": bool(user.get("disabled"))}
                for user in _load(_users_path()).values() if isinstance(user, dict)]

def admin_revoke(user_id: str) -> int:
    with _LOCK:
        tokens = _load(_tokens_path()); before = len(tokens)
        tokens = {k:v for k,v in tokens.items() if v.get("user") != user_id}; _save(_tokens_path(), tokens)
        return before - len(tokens)

def public_user(user: dict) -> dict:
    return {"id": user.get("id"), "displayName": user.get("displayName", ""), "role": user.get("role", "student")}

def seed_admin_from_env() -> None:
    username, password = os.environ.get("AMPHI_ADMIN_USER", ""), os.environ.get("AMPHI_ADMIN_PASSWORD", "")
    if not username or not password: return
    with _LOCK:
        users = _load(_users_path())
        if username not in users:
            users[username] = {"id": secrets.token_hex(16), "displayName": username, "role": "admin", "disabled": False, "password": _hash_password(password)}
            _save(_users_path(), users)
