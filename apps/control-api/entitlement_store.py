"""Minimal persistent entitlement store for Control Plane session limits."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from state_safety import mark_initialized, was_initialized


DEFAULT_PLAN = "default"
DEFAULT_MAX_CONCURRENT_SESSIONS = 1


class EntitlementStateError(RuntimeError):
    pass


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
                raw = json.load(handle)
        except FileNotFoundError:
            if was_initialized(self.path):
                raise EntitlementStateError(
                    f"entitlement state {self.path} disappeared after initialization"
                )
            self._entitlements = {}
            return
        except (json.JSONDecodeError, OSError) as exc:
            raise EntitlementStateError(
                f"cannot read entitlement state {self.path}"
            ) from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("entitlements"), dict):
            raise EntitlementStateError(
                f"invalid entitlement state payload in {self.path}"
            )
        if any(
            not isinstance(key, str) or not isinstance(value, dict)
            for key, value in raw["entitlements"].items()
        ):
            raise EntitlementStateError(
                f"invalid entitlement state record in {self.path}"
            )
        self._entitlements = dict(raw["entitlements"])
        mark_initialized(self.path)

    def _persist(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.state_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    {"entitlements": self._entitlements},
                    handle,
                    ensure_ascii=False,
                    sort_keys=True,
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
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
        if max_concurrent_sessions < 0:
            raise ValueError("max_concurrent_sessions must be at least 0")
        if not plan.strip():
            raise ValueError("plan must not be empty")
        with self._state_lock(exclusive=True):
            entitlement = {
                "id": f"user:{user_id}",
                "user_id": user_id,
                "plan": plan,
                "max_concurrent_sessions": max_concurrent_sessions,
                "updated_at": time.time(),
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
