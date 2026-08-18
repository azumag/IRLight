"""Persistent user store and session-token issuance.

Pure-python store (no FastAPI dependency) so the auth logic is unit-testable
without the web framework, matching the catalog_store.py convention. The API
layer in auth_api.py wraps this store and turns sessions into cookies.

Passwords are hashed with PBKDF2-HMAC-SHA256 (stdlib `hashlib`, no extra
dependency). Session identity is a random token; only its SHA-256 digest is
persisted, so a leaked state file does not hand out live sessions.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any


STATE_DIR = Path(os.getenv("STATE_DIR", "/state"))
USERS_PATH = STATE_DIR / "users.json"
AUTH_SESSIONS_PATH = STATE_DIR / "auth_sessions.json"

PBKDF2_ITERATIONS = 260_000
DEFAULT_SESSION_TTL_SECONDS = 7 * 24 * 3600


class AuthError(Exception):
    pass


class EmailAlreadyRegistered(AuthError):
    pass


class InvalidCredentials(AuthError):
    pass


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else default
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _default_users() -> dict[str, Any]:
    return {"users": {}, "email_index": {}}


def _default_sessions() -> dict[str, Any]:
    return {"sessions": {}}


def ensure_auth_state() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not USERS_PATH.exists():
        atomic_write_json(USERS_PATH, _default_users())
    if not AUTH_SESSIONS_PATH.exists():
        atomic_write_json(AUTH_SESSIONS_PATH, _default_sessions())


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, int(iterations)
        )
        return hmac.compare_digest(expected.hex(), digest_hex)
    except (ValueError, AttributeError):
        return False


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _public_user(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": user["id"],
        "email": user["email"],
        "display_name": user.get("display_name"),
        "role": user.get("role", "user"),
        "status": user.get("status", "active"),
        "created_at": user.get("created_at"),
        "updated_at": user.get("updated_at"),
    }


def register_user(
    *, email: str, password: str, display_name: str | None = None
) -> dict[str, Any]:
    normalized = _normalize_email(email)
    if "@" not in normalized or normalized.startswith("@") or normalized.endswith("@"):
        raise AuthError("invalid email address")
    if len(password) < 8:
        raise AuthError("password must be at least 8 characters")

    users = read_json(USERS_PATH, _default_users())
    if normalized in users.get("email_index", {}):
        raise EmailAlreadyRegistered(normalized)

    user_id = str(uuid.uuid4())
    now = time.time()
    user = {
        "id": user_id,
        "email": normalized,
        "password_hash": _hash_password(password),
        "display_name": display_name,
        "role": "user",
        "status": "active",
        "created_at": now,
        "updated_at": now,
    }
    users.setdefault("users", {})[user_id] = user
    users.setdefault("email_index", {})[normalized] = user_id
    atomic_write_json(USERS_PATH, users)
    return _public_user(user)


def authenticate_user(*, email: str, password: str) -> dict[str, Any]:
    normalized = _normalize_email(email)
    users = read_json(USERS_PATH, _default_users())
    user_id = users.get("email_index", {}).get(normalized)
    user = users.get("users", {}).get(user_id) if user_id else None
    # Always run a password hash, even for an unknown email, so responses
    # for unknown vs. wrong-password take comparable time.
    stored_hash = user["password_hash"] if user else _hash_password("placeholder")
    password_ok = _verify_password(password, stored_hash)
    if user is None or not password_ok or user.get("status") != "active":
        raise InvalidCredentials("invalid email or password")
    return _public_user(user)


def get_user(user_id: str) -> dict[str, Any] | None:
    users = read_json(USERS_PATH, _default_users())
    user = users.get("users", {}).get(user_id)
    return _public_user(user) if user else None


def create_session(
    user_id: str, *, ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS
) -> dict[str, Any]:
    token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(24)
    now = time.time()
    sessions = read_json(AUTH_SESSIONS_PATH, _default_sessions())
    sessions.setdefault("sessions", {})[_hash_token(token)] = {
        "user_id": user_id,
        "csrf_token": csrf_token,
        "created_at": now,
        "expires_at": now + ttl_seconds,
    }
    atomic_write_json(AUTH_SESSIONS_PATH, sessions)
    return {"token": token, "csrf_token": csrf_token, "expires_at": now + ttl_seconds}


def get_session_user(token: str) -> dict[str, Any] | None:
    sessions = read_json(AUTH_SESSIONS_PATH, _default_sessions())
    record = sessions.get("sessions", {}).get(_hash_token(token))
    if record is None:
        return None
    if float(record.get("expires_at", 0)) < time.time():
        return None
    user = get_user(str(record.get("user_id")))
    if user is None or user.get("status") != "active":
        return None
    return {"user": user, "csrf_token": record["csrf_token"]}


def revoke_session(token: str) -> None:
    sessions = read_json(AUTH_SESSIONS_PATH, _default_sessions())
    sessions.get("sessions", {}).pop(_hash_token(token), None)
    atomic_write_json(AUTH_SESSIONS_PATH, sessions)
