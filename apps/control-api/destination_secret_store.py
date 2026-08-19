"""Encrypted-at-rest destination secrets for egress publish credentials."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


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
    """

    def __init__(
        self,
        state_dir: str | os.PathLike[str] | None = None,
        *,
        master_key: bytes | str | None = None,
    ) -> None:
        self.state_dir = Path(state_dir or os.getenv("STATE_DIR", "/state"))
        self.path = self.state_dir / "destination_secrets.json"
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
        self._load()

    @staticmethod
    def _key(user_id: str, secret_ref: str) -> str:
        value = f"{user_id}\0{secret_ref}".encode("utf-8")
        return hashlib.sha256(value).hexdigest()

    def _load(self) -> None:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                raw = json.load(handle)
            records = raw.get("secrets", {}) if isinstance(raw, dict) else {}
            self._records = {
                str(key): value
                for key, value in records.items()
                if isinstance(key, str) and isinstance(value, dict)
            }
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            self._records = {}

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
        with self.lock:
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
        with self.lock:
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
        with self.lock:
            return record_key in self._records

    def delete(self, *, user_id: str, secret_ref: str) -> bool:
        record_key = self._key(user_id, secret_ref)
        with self.lock:
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
