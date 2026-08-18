"""Minimal persistent entitlement store for Control Plane session limits."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any


DEFAULT_PLAN = "default"
DEFAULT_MAX_CONCURRENT_SESSIONS = 1


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
        self.lock = threading.Lock()
        self._entitlements: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                raw = json.load(handle)
            data = raw if isinstance(raw, dict) else {}
            self._entitlements = {
                str(key): value
                for key, value in data.get("entitlements", {}).items()
                if isinstance(value, dict)
            }
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            self._entitlements = {}

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
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def get(self, user_id: str) -> dict[str, Any]:
        with self.lock:
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
        with self.lock:
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
