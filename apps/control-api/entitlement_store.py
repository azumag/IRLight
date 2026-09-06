"""Minimal persistent entitlement store for Control Plane session limits."""

from __future__ import annotations

import fcntl
import json
import math
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from state_safety import load_json_authority, mark_initialized, was_initialized


DEFAULT_PLAN = "default"
DEFAULT_MAX_CONCURRENT_SESSIONS = 1


class EntitlementStateError(RuntimeError):
    pass


def _reject_non_finite_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value} is not allowed")


def _require_nonempty_string(
    record: dict[str, Any], field: str, *, context: str
) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value:
        raise EntitlementStateError(f"{context} has invalid {field}")
    return value


def _require_nonnegative_int(
    record: dict[str, Any], field: str, *, context: str
) -> int:
    value = record.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EntitlementStateError(f"{context} has invalid {field}")
    return value


def _require_finite_number(
    record: dict[str, Any], field: str, *, context: str
) -> float:
    value = record.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EntitlementStateError(f"{context} has invalid {field}")
    try:
        number = float(value)
    except (OverflowError, ValueError):
        raise EntitlementStateError(f"{context} has invalid {field}") from None
    if not math.isfinite(number):
        raise EntitlementStateError(f"{context} has invalid {field}")
    return number


def _default_limit() -> int:
    raw = os.getenv("IRLIGHT_DEFAULT_MAX_CONCURRENT_SESSIONS", str(DEFAULT_MAX_CONCURRENT_SESSIONS))
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_CONCURRENT_SESSIONS
    return max(0, value)


class EntitlementStore:
    def __init__(self, state_dir: str | os.PathLike[str] | None = None) -> None:
        self.state_dir = Path(state_dir or os.getenv("STATE_DIR", "/state"))
        self.path = self.state_dir / "entitlements.json"
        self.lock_path = self.state_dir / ".entitlements.lock"
        self.lock = threading.Lock()
        self._entitlements: dict[str, dict[str, Any]] = {}
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
            except EntitlementStateError:
                raise
            except OSError as exc:
                raise EntitlementStateError(
                    f"cannot lock entitlement state {self.path}"
                ) from exc

    def _load(self) -> None:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                raw = load_json_authority(
                    handle,
                    parse_constant=_reject_non_finite_json_constant,
                )
        except FileNotFoundError:
            if was_initialized(self.path):
                raise EntitlementStateError(
                    f"entitlement state {self.path} disappeared after initialization"
                )
            self._entitlements = {}
            return
        except (ValueError, OSError) as exc:
            raise EntitlementStateError(
                f"cannot read entitlement state {self.path}"
            ) from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("entitlements"), dict):
            raise EntitlementStateError(
                f"invalid entitlement state payload in {self.path}"
            )
        entitlements = raw["entitlements"]
        self._validate_entitlements(entitlements)
        self._entitlements = dict(entitlements)
        mark_initialized(self.path)

    @staticmethod
    def _validate_entitlements(entitlements: dict[Any, Any]) -> None:
        for user_id, record in entitlements.items():
            if (
                not isinstance(user_id, str)
                or not user_id
                or not isinstance(record, dict)
            ):
                raise EntitlementStateError("invalid entitlement state record")

            stored_id = _require_nonempty_string(
                record, "id", context="entitlement record"
            )
            stored_user_id = _require_nonempty_string(
                record, "user_id", context="entitlement record"
            )
            plan = _require_nonempty_string(
                record, "plan", context="entitlement record"
            )
            if not plan.strip():
                raise EntitlementStateError("entitlement record has invalid plan")
            _require_nonnegative_int(
                record, "max_concurrent_sessions", context="entitlement record"
            )
            _require_finite_number(
                record, "updated_at", context="entitlement record"
            )

            if stored_user_id != user_id:
                raise EntitlementStateError(
                    "entitlement record user_id does not match its key"
                )
            if stored_id != f"user:{user_id}":
                raise EntitlementStateError(
                    "entitlement record id does not match its user_id"
                )

    def _persist(self) -> None:
        self._validate_entitlements(self._entitlements)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.state_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                try:
                    json.dump(
                        {"entitlements": self._entitlements},
                        handle,
                        ensure_ascii=False,
                        sort_keys=True,
                        allow_nan=False,
                    )
                except (TypeError, ValueError) as exc:
                    raise EntitlementStateError(
                        "entitlement state cannot be serialized"
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

    def get(self, user_id: str) -> dict[str, Any]:
        with self._state_lock(exclusive=False):
            stored = self._entitlements.get(user_id)
            if stored is None:
                return {
                    "id": f"default:{user_id}",
                    "user_id": user_id,
                    "plan": DEFAULT_PLAN,
                    "max_concurrent_sessions": _default_limit(),
                    "updated_at": None,
                }
            return dict(stored)

    def set(
        self,
        user_id: str,
        *,
        max_concurrent_sessions: int,
        plan: str = DEFAULT_PLAN,
    ) -> dict[str, Any]:
        if not isinstance(user_id, str) or not user_id:
            raise ValueError("user_id must not be empty")
        if (
            isinstance(max_concurrent_sessions, bool)
            or not isinstance(max_concurrent_sessions, int)
            or max_concurrent_sessions < 0
        ):
            raise ValueError("max_concurrent_sessions must be a non-negative integer")
        if not isinstance(plan, str) or not plan.strip():
            raise ValueError("plan must not be empty")
        with self._state_lock(exclusive=True):
            updated_at = time.time()
            if not math.isfinite(updated_at):
                raise EntitlementStateError("entitlement updated_at is not finite")
            entitlement = {
                "id": f"user:{user_id}",
                "user_id": user_id,
                "plan": plan,
                "max_concurrent_sessions": max_concurrent_sessions,
                "updated_at": updated_at,
            }
            self._entitlements[user_id] = entitlement
            self._persist()
            return dict(entitlement)


_DEFAULT_STORE: EntitlementStore | None = None


def default_entitlement_store() -> EntitlementStore:
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        _DEFAULT_STORE = EntitlementStore()
    return _DEFAULT_STORE
