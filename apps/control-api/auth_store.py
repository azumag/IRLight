"""Persistent user store and session-token issuance.

Pure-python store (no FastAPI dependency) so the auth logic is unit-testable
without the web framework, matching the catalog_store.py convention. The API
layer in auth_api.py wraps this store and turns sessions into cookies.

Passwords are hashed with PBKDF2-HMAC-SHA256 (stdlib `hashlib`, no extra
dependency). Session identity is a random token; only its SHA-256 digest is
persisted, so a leaked state file does not hand out live sessions.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from state_safety import mark_initialized, was_initialized


STATE_DIR = Path(os.getenv("STATE_DIR", "/state"))
USERS_PATH = STATE_DIR / "users.json"
AUTH_SESSIONS_PATH = STATE_DIR / "auth_sessions.json"
AUTH_LOCK_PATH = STATE_DIR / ".auth-state.lock"
AUTH_LOCK = threading.RLock()

PBKDF2_ITERATIONS = 260_000
DEFAULT_SESSION_TTL_SECONDS = 7 * 24 * 3600


class AuthError(Exception):
    pass


class EmailAlreadyRegistered(AuthError):
    pass


class InvalidCredentials(AuthError):
    pass


class AuthStateError(AuthError):
    pass


@contextmanager
def _state_lock(*, exclusive: bool):
    with AUTH_LOCK:
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            with AUTH_LOCK_PATH.open("a+", encoding="utf-8") as handle:
                operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
                fcntl.flock(handle.fileno(), operation)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except AuthStateError:
            raise
        except OSError as exc:
            raise AuthStateError("authentication state cannot be locked") from exc


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        # Initialization is a durable write-intent fuse. It must be armed
        # before replacing the authority file so a crash cannot publish state
        # without leaving evidence that implicit empty recreation is forbidden.
        mark_initialized(path)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError:
        if was_initialized(path):
            raise AuthStateError(
                f"authentication state {path} disappeared after initialization"
            )
        return default
    except (json.JSONDecodeError, OSError) as exc:
        raise AuthStateError(f"authentication state {path} cannot be read") from exc
    if not isinstance(value, dict):
        raise AuthStateError(f"authentication state {path} has invalid structure")
    return value


def _default_users() -> dict[str, Any]:
    return {"users": {}, "email_index": {}}


def _default_sessions() -> dict[str, Any]:
    return {"sessions": {}}


def ensure_auth_state() -> None:
    with _state_lock(exclusive=True):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        if not USERS_PATH.exists():
            if was_initialized(USERS_PATH):
                raise AuthStateError("user state disappeared after initialization")
            atomic_write_json(USERS_PATH, _default_users())
        else:
            _validate_users(read_json(USERS_PATH, _default_users()))
            mark_initialized(USERS_PATH)
        if not AUTH_SESSIONS_PATH.exists():
            if was_initialized(AUTH_SESSIONS_PATH):
                raise AuthStateError(
                    "authentication session state disappeared after initialization"
                )
            atomic_write_json(AUTH_SESSIONS_PATH, _default_sessions())
        else:
            _validate_sessions(read_json(AUTH_SESSIONS_PATH, _default_sessions()))
            mark_initialized(AUTH_SESSIONS_PATH)


def _validate_users(value: dict[str, Any]) -> dict[str, Any]:
    users = value.get("users")
    email_index = value.get("email_index")
    if not isinstance(users, dict) or not isinstance(email_index, dict):
        raise AuthStateError("user state has invalid structure")
    if any(not isinstance(key, str) or not isinstance(item, dict) for key, item in users.items()):
        raise AuthStateError("user state has an invalid record")
    if any(not isinstance(email, str) or not isinstance(user_id, str) for email, user_id in email_index.items()):
        raise AuthStateError("user email index has an invalid record")
    return value


def _validate_sessions(value: dict[str, Any]) -> dict[str, Any]:
    sessions = value.get("sessions")
    if not isinstance(sessions, dict):
        raise AuthStateError("authentication session state has invalid structure")
    if any(not isinstance(key, str) or not isinstance(item, dict) for key, item in sessions.items()):
        raise AuthStateError("authentication session state has an invalid record")
    return value


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


# A single fixed dummy record keeps unknown-email authentication on the same
# PBKDF2 verification path as a real user without recalculating a fresh hash
# (and without holding the state lock while doing expensive work).
_DUMMY_PASSWORD_HASH = _hash_password("placeholder", salt=b"\x00" * 16)


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

    # PBKDF2 is intentionally performed before taking the process/file lock;
    # concurrent logins and session operations must not be serialized behind
    # the deliberately expensive password derivation.
    password_hash = _hash_password(password)
    with _state_lock(exclusive=True):
        users = _validate_users(read_json(USERS_PATH, _default_users()))
        if normalized in users["email_index"]:
            raise EmailAlreadyRegistered(normalized)

        user_id = str(uuid.uuid4())
        now = time.time()
        user = {
            "id": user_id,
            "email": normalized,
            "password_hash": password_hash,
            "display_name": display_name,
            "role": "user",
            "status": "active",
            "created_at": now,
            "updated_at": now,
        }
        users["users"][user_id] = user
        users["email_index"][normalized] = user_id
        atomic_write_json(USERS_PATH, users)
        return _public_user(user)


def authenticate_user(*, email: str, password: str) -> dict[str, Any]:
    normalized = _normalize_email(email)
    with _state_lock(exclusive=False):
        users = _validate_users(read_json(USERS_PATH, _default_users()))
        user_id = users["email_index"].get(normalized)
        user = users["users"].get(user_id) if user_id else None
    # Always run exactly one PBKDF2 verification, even for an unknown email,
    # so responses for unknown vs. wrong-password take comparable time.
    stored_hash = user["password_hash"] if user else _DUMMY_PASSWORD_HASH
    password_ok = _verify_password(password, stored_hash)
    if user is None or not password_ok or user.get("status") != "active":
        raise InvalidCredentials("invalid email or password")
    return _public_user(user)


def get_user(user_id: str) -> dict[str, Any] | None:
    with _state_lock(exclusive=False):
        users = _validate_users(read_json(USERS_PATH, _default_users()))
        user = users["users"].get(user_id)
        return _public_user(user) if user else None


def create_session(
    user_id: str, *, ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS
) -> dict[str, Any]:
    token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(24)
    now = time.time()
    with _state_lock(exclusive=True):
        sessions = _validate_sessions(
            read_json(AUTH_SESSIONS_PATH, _default_sessions())
        )
        sessions["sessions"][_hash_token(token)] = {
            "user_id": user_id,
            "csrf_token": csrf_token,
            "created_at": now,
            "expires_at": now + ttl_seconds,
        }
        atomic_write_json(AUTH_SESSIONS_PATH, sessions)
    return {"token": token, "csrf_token": csrf_token, "expires_at": now + ttl_seconds}


def get_session_user(token: str) -> dict[str, Any] | None:
    with _state_lock(exclusive=False):
        sessions = _validate_sessions(
            read_json(AUTH_SESSIONS_PATH, _default_sessions())
        )
        stored = sessions["sessions"].get(_hash_token(token))
        record = dict(stored) if isinstance(stored, dict) else None
        if record is None:
            return None
        try:
            expired = float(record.get("expires_at", 0)) < time.time()
        except (TypeError, ValueError):
            return None
        if expired:
            return None
    user = get_user(str(record.get("user_id")))
    if user is None or user.get("status") != "active":
        return None
    return {"user": user, "csrf_token": record["csrf_token"]}


def revoke_session(token: str) -> None:
    with _state_lock(exclusive=True):
        sessions = _validate_sessions(
            read_json(AUTH_SESSIONS_PATH, _default_sessions())
        )
        if sessions["sessions"].pop(_hash_token(token), None) is not None:
            atomic_write_json(AUTH_SESSIONS_PATH, sessions)
