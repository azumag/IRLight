"""Persistent, one-way-hashed ingest credentials for MediaMTX publish auth."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable


SUPPORTED_PROTOCOLS = {"rtmp", "srt"}
DEFAULT_TTL_SECONDS = 12 * 3600


class IngestCredentialError(RuntimeError):
    pass


class IngestCredentialStore:
    """JSON-backed credential store.

    Raw publisher secrets are returned once from ``issue`` and are never
    persisted. Since generated secrets contain at least 256 bits of entropy,
    an unsalted SHA-256 digest is sufficient for lookup/verification and does
    not create a practical offline guessing target.
    """

    def __init__(self, state_dir: str | os.PathLike[str] | None = None) -> None:
        self.state_dir = Path(state_dir or os.getenv("STATE_DIR", "/state"))
        self.path = self.state_dir / "ingest_credentials.json"
        self.lock = threading.Lock()
        self._credentials: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                raw = json.load(handle)
            data = raw if isinstance(raw, dict) else {}
            self._credentials = {
                str(key): value
                for key, value in data.get("credentials", {}).items()
                if isinstance(value, dict)
            }
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            self._credentials = {}

    def _persist(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.state_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    {"credentials": self._credentials},
                    handle,
                    ensure_ascii=False,
                    sort_keys=True,
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    @staticmethod
    def _digest(secret: str) -> str:
        return hashlib.sha256(secret.encode("utf-8")).hexdigest()

    @staticmethod
    def _normalize_protocols(protocols: Iterable[str]) -> list[str]:
        values = sorted({str(value).lower() for value in protocols})
        if not values or any(value not in SUPPORTED_PROTOCOLS for value in values):
            raise ValueError("protocols must contain rtmp and/or srt")
        return values

    def issue(
        self,
        *,
        session_id: str,
        user_id: str,
        protocols: Iterable[str] = ("rtmp", "srt"),
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        now: float | None = None,
    ) -> tuple[dict[str, Any], str]:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        normalized = self._normalize_protocols(protocols)
        issued_at = time.time() if now is None else now
        secret = secrets.token_urlsafe(32)
        credential_id = str(uuid.uuid4())
        record = {
            "id": credential_id,
            "session_id": session_id,
            "user_id": user_id,
            "username": session_id,
            "secret_sha256": self._digest(secret),
            "protocols": normalized,
            "created_at": issued_at,
            "expires_at": issued_at + ttl_seconds,
            "revoked_at": None,
            "last_authenticated_at": None,
        }

        with self.lock:
            # Rotation is deliberate: at most one active ingest credential is
            # accepted per Session, so an old copied key stops working as soon
            # as the user asks for a replacement.
            for existing in self._credentials.values():
                if existing.get("session_id") == session_id and existing.get("revoked_at") is None:
                    existing["revoked_at"] = issued_at
            self._credentials[credential_id] = record
            self._persist()
        return self.public_record(record), secret

    def verify(
        self,
        *,
        username: str,
        secret: str,
        protocol: str,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        current = time.time() if now is None else now
        protocol = protocol.lower()
        if protocol not in SUPPORTED_PROTOCOLS or not username or not secret:
            return None
        candidate = self._digest(secret)

        with self.lock:
            for record in self._credentials.values():
                if record.get("username") != username:
                    continue
                if record.get("revoked_at") is not None:
                    continue
                try:
                    expires_at = float(record.get("expires_at", 0))
                except (TypeError, ValueError):
                    continue
                if expires_at <= current:
                    continue
                if protocol not in record.get("protocols", []):
                    continue
                stored = str(record.get("secret_sha256", ""))
                if not secrets.compare_digest(stored, candidate):
                    continue
                record["last_authenticated_at"] = current
                self._persist()
                return self.public_record(record)
        return None

    def revoke(self, credential_id: str, *, user_id: str | None = None) -> dict[str, Any]:
        with self.lock:
            record = self._credentials.get(credential_id)
            if record is None or (user_id is not None and record.get("user_id") != user_id):
                raise KeyError(credential_id)
            if record.get("revoked_at") is None:
                record["revoked_at"] = time.time()
                self._persist()
            return self.public_record(record)

    def revoke_session(self, session_id: str, *, now: float | None = None) -> int:
        revoked_at = time.time() if now is None else now
        changed = 0
        with self.lock:
            for record in self._credentials.values():
                if record.get("session_id") == session_id and record.get("revoked_at") is None:
                    record["revoked_at"] = revoked_at
                    changed += 1
            if changed:
                self._persist()
        return changed

    def active_for_session(self, session_id: str, *, now: float | None = None) -> dict[str, Any] | None:
        current = time.time() if now is None else now
        with self.lock:
            for record in reversed(list(self._credentials.values())):
                if record.get("session_id") != session_id or record.get("revoked_at") is not None:
                    continue
                try:
                    if float(record.get("expires_at", 0)) <= current:
                        continue
                except (TypeError, ValueError):
                    continue
                return self.public_record(record)
        return None

    @staticmethod
    def public_record(record: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in record.items() if key != "secret_sha256"}


_DEFAULT_STORE: IngestCredentialStore | None = None


def default_ingest_store() -> IngestCredentialStore:
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        _DEFAULT_STORE = IngestCredentialStore()
    return _DEFAULT_STORE
