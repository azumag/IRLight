"""Persistent, one-way-hashed ingest credentials for MediaMTX publish auth."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import secrets
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from state_safety import mark_initialized, was_initialized


CREDENTIAL_SCOPES = {"INGEST", "RELAY_CLIENT"}
SUPPORTED_PROTOCOLS = {"rtmp", "srt"}
DEFAULT_TTL_SECONDS = 12 * 3600


class IngestCredentialError(RuntimeError):
    pass


def _reject_non_finite_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value} is not allowed")


def _finite_argument(value: Any, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite number")
    try:
        number = float(value)
    except (OverflowError, ValueError):
        raise ValueError(f"{field} must be a finite number") from None
    if not math.isfinite(number):
        raise ValueError(f"{field} must be a finite number")
    return number


def _runtime_timestamp(value: float | None, *, field: str) -> float:
    return _finite_argument(time.time() if value is None else value, field=field)


def _require_nonempty_string(
    record: dict[str, Any], field: str, *, context: str
) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value:
        raise IngestCredentialError(f"{context} has invalid {field}")
    return value


def _require_finite_number(
    record: dict[str, Any], field: str, *, context: str
) -> float:
    try:
        return _finite_argument(record.get(field), field=field)
    except ValueError:
        raise IngestCredentialError(f"{context} has invalid {field}") from None


def _optional_finite_number(
    record: dict[str, Any], field: str, *, context: str
) -> float | None:
    if record.get(field) is None:
        return None
    return _require_finite_number(record, field, context=context)


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
        self.lock_path = self.state_dir / ".ingest-credentials.lock"
        self.lock = threading.Lock()
        self._credentials: dict[str, dict[str, Any]] = {}
        with self._state_lock(exclusive=False):
            pass

    @contextmanager
    def _state_lock(self, *, exclusive: bool):
        with self.lock:
            try:
                self.state_dir.mkdir(parents=True, exist_ok=True)
                with self.lock_path.open("a+", encoding="utf-8") as handle:
                    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
                    fcntl.flock(handle.fileno(), operation)
                    try:
                        self._load()
                        yield
                    finally:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except IngestCredentialError:
                raise
            except OSError as exc:
                raise IngestCredentialError(
                    f"cannot lock ingest credential state {self.path}: {exc}"
                ) from exc

    def _load(self) -> None:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                raw = json.load(
                    handle,
                    parse_constant=_reject_non_finite_json_constant,
                )
        except FileNotFoundError:
            if was_initialized(self.path):
                raise IngestCredentialError(
                    f"ingest credential state {self.path} disappeared after initialization"
                )
            self._credentials = {}
            return
        except (ValueError, OSError) as exc:
            raise IngestCredentialError(
                f"cannot read ingest credential state {self.path}: {exc}"
            ) from exc

        if not isinstance(raw, dict) or not isinstance(raw.get("credentials"), dict):
            raise IngestCredentialError(
                f"invalid ingest credential state payload in {self.path}"
            )
        credentials = raw["credentials"]
        self._validate_credentials(credentials)
        self._credentials = dict(credentials)
        mark_initialized(self.path)

    @staticmethod
    def _validate_credentials(credentials: dict[Any, Any]) -> None:
        for credential_id, record in credentials.items():
            if (
                not isinstance(credential_id, str)
                or not credential_id
                or not isinstance(record, dict)
            ):
                raise IngestCredentialError("invalid ingest credential record")

            stored_id = _require_nonempty_string(
                record, "id", context="ingest credential record"
            )
            session_id = _require_nonempty_string(
                record, "session_id", context="ingest credential record"
            )
            _require_nonempty_string(
                record, "user_id", context="ingest credential record"
            )
            username = _require_nonempty_string(
                record, "username", context="ingest credential record"
            )
            digest = _require_nonempty_string(
                record, "secret_sha256", context="ingest credential record"
            )

            if stored_id != credential_id:
                raise IngestCredentialError(
                    "ingest credential record id does not match its key"
                )
            if username != session_id:
                raise IngestCredentialError(
                    "ingest credential record username does not match session_id"
                )
            if len(digest) != 64:
                raise IngestCredentialError(
                    "ingest credential record has invalid secret_sha256"
                )
            try:
                bytes.fromhex(digest)
            except ValueError:
                raise IngestCredentialError(
                    "ingest credential record has invalid secret_sha256"
                ) from None

            # Records created before relay credentials were introduced did not
            # persist a scope. Keep that one explicit compatibility path while
            # rejecting unknown persisted scopes.
            scope = record.get("scope", "INGEST")
            if not isinstance(scope, str) or scope not in CREDENTIAL_SCOPES:
                raise IngestCredentialError("ingest credential record has invalid scope")

            protocols = record.get("protocols")
            if (
                not isinstance(protocols, list)
                or not protocols
                or any(
                    not isinstance(protocol, str)
                    or protocol not in SUPPORTED_PROTOCOLS
                    for protocol in protocols
                )
            ):
                raise IngestCredentialError(
                    "ingest credential record has invalid protocols"
                )

            created_at = _require_finite_number(
                record, "created_at", context="ingest credential record"
            )
            expires_at = _require_finite_number(
                record, "expires_at", context="ingest credential record"
            )
            if expires_at <= created_at:
                raise IngestCredentialError(
                    "ingest credential record has invalid expires_at"
                )
            _optional_finite_number(
                record, "revoked_at", context="ingest credential record"
            )
            _optional_finite_number(
                record,
                "last_authenticated_at",
                context="ingest credential record",
            )

    def _persist(self) -> None:
        self._validate_credentials(self._credentials)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.state_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                try:
                    json.dump(
                        {"credentials": self._credentials},
                        handle,
                        ensure_ascii=False,
                        sort_keys=True,
                        allow_nan=False,
                    )
                except (TypeError, ValueError) as exc:
                    raise IngestCredentialError(
                        "ingest credential state cannot be serialized"
                    ) from exc
                handle.flush()
                os.fsync(handle.fileno())
            mark_initialized(self.path)
            os.replace(temporary, self.path)
            directory_fd = os.open(self.state_dir, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
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
        scope: str = "INGEST",
        protocols: Iterable[str] = ("rtmp", "srt"),
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        now: float | None = None,
    ) -> tuple[dict[str, Any], str]:
        ttl = _finite_argument(ttl_seconds, field="ttl_seconds")
        if ttl <= 0:
            raise ValueError("ttl_seconds must be positive")
        if scope not in CREDENTIAL_SCOPES:
            raise ValueError(f"scope must be one of {sorted(CREDENTIAL_SCOPES)}")
        normalized = self._normalize_protocols(protocols)
        issued_at = _runtime_timestamp(now, field="now")
        secret = secrets.token_urlsafe(32)
        credential_id = str(uuid.uuid4())
        record = {
            "id": credential_id,
            "session_id": session_id,
            "user_id": user_id,
            "scope": scope,
            "username": session_id,
            "secret_sha256": self._digest(secret),
            "protocols": normalized,
            "created_at": issued_at,
            "expires_at": issued_at + ttl,
            "revoked_at": None,
            "last_authenticated_at": None,
        }

        with self._state_lock(exclusive=True):
            # Rotation is deliberate: at most one active ingest credential is
            # accepted per Session, so an old copied key stops working as soon
            # as the user asks for a replacement.
            for existing in self._credentials.values():
                if (
                    existing.get("session_id") == session_id
                    and existing.get("scope", "INGEST") == scope
                    and existing.get("revoked_at") is None
                ):
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
        scope: str = "INGEST",
    ) -> dict[str, Any] | None:
        current = _runtime_timestamp(now, field="now")
        protocol = protocol.lower()
        if (
            protocol not in SUPPORTED_PROTOCOLS
            or scope not in CREDENTIAL_SCOPES
            or not username
            or not secret
        ):
            return None
        candidate = self._digest(secret)

        with self._state_lock(exclusive=True):
            for record in self._credentials.values():
                if record.get("username") != username:
                    continue
                if record.get("scope", "INGEST") != scope:
                    continue
                if record.get("revoked_at") is not None:
                    continue
                if float(record["expires_at"]) <= current:
                    continue
                if protocol not in record["protocols"]:
                    continue
                stored = record["secret_sha256"]
                if not secrets.compare_digest(stored, candidate):
                    continue
                record["last_authenticated_at"] = current
                self._persist()
                return self.public_record(record)
        return None

    def revoke(self, credential_id: str, *, user_id: str | None = None) -> dict[str, Any]:
        with self._state_lock(exclusive=True):
            record = self._credentials.get(credential_id)
            if record is None or (user_id is not None and record.get("user_id") != user_id):
                raise KeyError(credential_id)
            if record.get("revoked_at") is None:
                record["revoked_at"] = time.time()
                self._persist()
            return self.public_record(record)

    def revoke_session(self, session_id: str, *, now: float | None = None) -> int:
        revoked_at = _runtime_timestamp(now, field="now")
        changed = 0
        with self._state_lock(exclusive=True):
            for record in self._credentials.values():
                if record.get("session_id") == session_id and record.get("revoked_at") is None:
                    record["revoked_at"] = revoked_at
                    changed += 1
            if changed:
                self._persist()
        return changed

    def active_for_session(
        self,
        session_id: str,
        *,
        now: float | None = None,
        scope: str = "INGEST",
    ) -> dict[str, Any] | None:
        current = _runtime_timestamp(now, field="now")
        with self._state_lock(exclusive=False):
            for record in reversed(list(self._credentials.values())):
                if (
                    record.get("session_id") != session_id
                    or record.get("scope", "INGEST") != scope
                    or record.get("revoked_at") is not None
                ):
                    continue
                if float(record["expires_at"]) <= current:
                    continue
                return self.public_record(record)
        return None

    def get(self, credential_id: str) -> dict[str, Any] | None:
        with self._state_lock(exclusive=False):
            record = self._credentials.get(credential_id)
            return self.public_record(record) if record is not None else None

    @staticmethod
    def public_record(record: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in record.items() if key != "secret_sha256"}


_DEFAULT_STORE: IngestCredentialStore | None = None


def default_ingest_store(
    state_dir: str | os.PathLike[str] | None = None,
) -> IngestCredentialStore:
    global _DEFAULT_STORE
    if state_dir is not None:
        return IngestCredentialStore(state_dir)
    if _DEFAULT_STORE is None:
        _DEFAULT_STORE = IngestCredentialStore()
    return _DEFAULT_STORE
