"""Encrypted-at-rest destination secrets for egress publish credentials."""

from __future__ import annotations

import hashlib
import fcntl
import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from state_safety import mark_initialized, was_initialized


class DestinationSecretError(RuntimeError):
    pass


class DestinationSecretConfigurationError(DestinationSecretError):
    pass


class DestinationSecretNotFound(DestinationSecretError):
    pass


def _read_master_key() -> bytes:
    file_path = os.getenv("IRLIGHT_SECRET_MASTER_KEY_FILE", "").strip()
    if file_path:
        try:
            value = Path(file_path).read_bytes().strip()
        except OSError as exc:
            raise DestinationSecretConfigurationError(
                "cannot read IRLIGHT_SECRET_MASTER_KEY_FILE"
            ) from exc
    else:
        value = os.getenv("IRLIGHT_SECRET_MASTER_KEY", "").strip().encode("ascii")
    if not value:
        raise DestinationSecretConfigurationError(
            "destination secret master key is not configured"
        )
    try:
        Fernet(value)
    except (ValueError, TypeError) as exc:
        raise DestinationSecretConfigurationError(
            "destination secret master key is invalid"
        ) from exc
    return value


class DestinationSecretStore:
    """Small encrypted JSON secret store for the current Control Plane spike.

    The ciphertext is authenticated with Fernet. The encryption key never
    enters the JSON state file; production deployments should mount it through
    ``IRLIGHT_SECRET_MASTER_KEY_FILE``. Secret values are only returned by
    ``resolve`` and are never included in catalog responses.

    Existing state fails closed: only a missing state file means an empty
    store. Corrupt, unreadable, or structurally invalid state is never replaced
    by a silently empty store.
    """

    def __init__(
        self,
        state_dir: str | os.PathLike[str] | None = None,
        *,
        master_key: bytes | str | None = None,
    ) -> None:
        self.state_dir = Path(state_dir or os.getenv("STATE_DIR", "/state"))
        self.path = self.state_dir / "destination_secrets.json"
        self.lock_path = self.state_dir / ".destination-secrets.lock"
        key = master_key if master_key is not None else _read_master_key()
        if isinstance(key, str):
            key = key.encode("ascii")
        try:
            self.fernet = Fernet(key)
        except (ValueError, TypeError) as exc:
            raise DestinationSecretConfigurationError(
                "destination secret master key is invalid"
            ) from exc
        self.lock = threading.Lock()
        self._records: dict[str, dict[str, Any]] = {}
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
            except DestinationSecretError:
                raise
            except OSError as exc:
                raise DestinationSecretError(
                    "destination secret state cannot be locked"
                ) from exc

    @staticmethod
    def _key(user_id: str, secret_ref: str) -> str:
        value = f"{user_id}\0{secret_ref}".encode("utf-8")
        return hashlib.sha256(value).hexdigest()

    def _load(self) -> None:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except FileNotFoundError:
            if was_initialized(self.path):
                raise DestinationSecretError(
                    "destination secret state disappeared after initialization"
                )
            self._records = {}
            return
        except json.JSONDecodeError as exc:
            raise DestinationSecretError(
                "destination secret state contains invalid JSON"
            ) from exc
        except OSError as exc:
            raise DestinationSecretError(
                "destination secret state cannot be read"
            ) from exc

        if not isinstance(raw, dict):
            raise DestinationSecretError("destination secret state has invalid structure")
        records = raw.get("secrets")
        if not isinstance(records, dict):
            raise DestinationSecretError("destination secret state has invalid structure")

        validated: dict[str, dict[str, Any]] = {}
        for record_key, record in records.items():
            if not isinstance(record_key, str) or not isinstance(record, dict):
                raise DestinationSecretError(
                    "destination secret state has invalid record"
                )
            user_id = record.get("user_id")
            secret_ref = record.get("secret_ref")
            ciphertext = record.get("ciphertext")
            if (
                not isinstance(user_id, str)
                or not user_id
                or not isinstance(secret_ref, str)
                or not secret_ref
                or not isinstance(ciphertext, str)
                or not ciphertext
            ):
                raise DestinationSecretError(
                    "destination secret state has invalid record"
                )
            if record_key != self._key(user_id, secret_ref):
                raise DestinationSecretError(
                    "destination secret state has mismatched record key"
                )
            validated[record_key] = record
        self._records = validated
        mark_initialized(self.path)

    def _persist(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.state_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    {"secrets": self._records},
                    handle,
                    ensure_ascii=False,
                    sort_keys=True,
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
            directory_fd = os.open(self.state_dir, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            mark_initialized(self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def put(
        self,
        *,
        user_id: str,
        secret_ref: str,
        value: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        if not user_id or not secret_ref or not value:
            raise ValueError("user_id, secret_ref and secret value are required")
        current = time.time() if now is None else now
        record_key = self._key(user_id, secret_ref)
        ciphertext = self.fernet.encrypt(value.encode("utf-8")).decode("ascii")
        with self._state_lock(exclusive=True):
            existing = self._records.get(record_key)
            created_at = (
                float(existing.get("created_at", current))
                if isinstance(existing, dict)
                else current
            )
            record = {
                "user_id": user_id,
                "secret_ref": secret_ref,
                "ciphertext": ciphertext,
                "created_at": created_at,
                "updated_at": current,
            }
            self._records[record_key] = record
            self._persist()
        return {
            "secret_ref": secret_ref,
            "configured": True,
            "created_at": created_at,
            "updated_at": current,
        }

    def resolve(self, *, user_id: str, secret_ref: str) -> str:
        record_key = self._key(user_id, secret_ref)
        with self._state_lock(exclusive=False):
            record = self._records.get(record_key)
            if record is None:
                raise DestinationSecretNotFound(secret_ref)
            ciphertext = str(record.get("ciphertext", ""))
        try:
            plaintext = self.fernet.decrypt(ciphertext.encode("ascii"))
        except (InvalidToken, ValueError, UnicodeEncodeError) as exc:
            raise DestinationSecretError("destination secret cannot be decrypted") from exc
        try:
            return plaintext.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DestinationSecretError("destination secret is not valid UTF-8") from exc

    def configured(self, *, user_id: str, secret_ref: str) -> bool:
        record_key = self._key(user_id, secret_ref)
        with self._state_lock(exclusive=False):
            return record_key in self._records

    def delete(self, *, user_id: str, secret_ref: str) -> bool:
        record_key = self._key(user_id, secret_ref)
        with self._state_lock(exclusive=True):
            removed = self._records.pop(record_key, None) is not None
            if removed:
                self._persist()
            return removed


_DEFAULT_STORE: DestinationSecretStore | None = None


def default_destination_secret_store() -> DestinationSecretStore:
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        _DEFAULT_STORE = DestinationSecretStore()
    return _DEFAULT_STORE
