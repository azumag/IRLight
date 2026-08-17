"""Persistent Session state and the Session state machine.

The state machine follows the Phase B design:

    STOPPED -> PROVISIONING -> BOOTSTRAPPING -> READY_WAIT_INGEST
             -> LIVE -> HOLDING -> STOPPING -> FINISHED

Any active state can fail into FAILED_CLEANUP (reaper still owns provider
resources) and then FAILED once cleanup completed. STOPPING is terminal for
new work; cleanup must finish before FINISHED.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any


SESSION_STATES = {
    "STOPPED",
    "PROVISIONING",
    "BOOTSTRAPPING",
    "READY_WAIT_INGEST",
    "LIVE",
    "HOLDING",
    "STOPPING",
    "FINISHED",
    "FAILED_CLEANUP",
    "FAILED",
}

ACTIVE_STATES = {
    "PROVISIONING",
    "BOOTSTRAPPING",
    "READY_WAIT_INGEST",
    "LIVE",
    "HOLDING",
}

TERMINAL_STATES = {"STOPPED", "FINISHED", "FAILED"}

# Allowed transitions from each state. Idempotent retries of the same
# operation are handled by the caller, not by widening these sets.
TRANSITIONS: dict[str, set[str]] = {
    "STOPPED": {"PROVISIONING"},
    "PROVISIONING": {"BOOTSTRAPPING", "STOPPING", "FAILED_CLEANUP"},
    "BOOTSTRAPPING": {"READY_WAIT_INGEST", "STOPPING", "FAILED_CLEANUP"},
    "READY_WAIT_INGEST": {"LIVE", "STOPPING", "FAILED_CLEANUP"},
    "LIVE": {"HOLDING", "STOPPING", "FAILED_CLEANUP"},
    "HOLDING": {"LIVE", "STOPPING", "FAILED_CLEANUP"},
    "STOPPING": {"FINISHED", "FAILED_CLEANUP"},
    "FAILED_CLEANUP": {"FAILED", "STOPPING"},
    "FINISHED": set(),
    "FAILED": set(),
}


class InvalidTransition(RuntimeError):
    pass


def new_session_id() -> str:
    return str(uuid.uuid4())


def new_session(
    *,
    user_id: str,
    environment: str = "dev",
    absolute_deadline_hours: float | None = None,
) -> dict[str, Any]:
    now = time.time()
    return {
        "session_id": new_session_id(),
        "user_id": user_id,
        "environment": environment,
        "status": "STOPPED",
        "idempotency_key": None,
        "version": 0,
        "provider": None,
        "provider_volume_id": None,
        "provider_server_id": None,
        "provider_public_ipv4": None,
        "provisioning_started_at": None,
        "ready_at": None,
        "first_ingest_at": None,
        "last_ingest_at": None,
        "hold_deadline_at": None,
        "absolute_deadline_at": (
            now + absolute_deadline_hours * 3600 if absolute_deadline_hours else None
        ),
        "cleanup_pending": False,
        "failure_reason": None,
        "events": [],
        "created_at": now,
        "updated_at": now,
    }


class SessionStore:
    """JSON-file persisted session store with an in-process lock.

    One lock protects both the in-memory cache and the file, matching the
    existing control-api pattern (single uvicorn worker).
    """

    def __init__(self, state_dir: str | os.PathLike[str] | None = None) -> None:
        self.state_dir = Path(state_dir or os.getenv("STATE_DIR", "/state"))
        self.path = self.state_dir / "sessions.json"
        self.lock = threading.Lock()
        self._sessions: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                raw = json.load(handle)
            data = raw if isinstance(raw, dict) else {}
            self._sessions = {
                str(key): value
                for key, value in data.get("sessions", {}).items()
                if isinstance(value, dict)
            }
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            self._sessions = {}

    def _persist(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.state_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    {"sessions": self._sessions},
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

    def create(self, session_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        with self.lock:
            session = new_session(**kwargs)
            if session_id is not None:
                if session_id in self._sessions:
                    return dict(self._sessions[session_id])
                session["session_id"] = session_id
            self._sessions[session["session_id"]] = session
            self._persist()
            return dict(session)

    def get(self, session_id: str) -> dict[str, Any] | None:
        with self.lock:
            value = self._sessions.get(session_id)
            return dict(value) if value else None

    def list(self) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(value) for value in self._sessions.values()]

    def update(self, session_id: str, **changes: Any) -> dict[str, Any]:
        with self.lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise KeyError(session_id)
            session.update(changes)
            session["updated_at"] = time.time()
            self._persist()
            return dict(session)

    def transition(
        self,
        session_id: str,
        new_state: str,
        *,
        allow_from: set[str] | None = None,
        **changes: Any,
    ) -> dict[str, Any]:
        if new_state not in SESSION_STATES:
            raise InvalidTransition(f"unknown state: {new_state}")
        with self.lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise KeyError(session_id)
            current = str(session.get("status"))
            if allow_from is not None:
                if current not in allow_from:
                    raise InvalidTransition(f"{current} -> {new_state} not allowed")
            else:
                allowed = TRANSITIONS.get(current, set())
                if new_state not in allowed:
                    raise InvalidTransition(f"{current} -> {new_state} not allowed")
            session.update(changes)
            session["status"] = new_state
            session["updated_at"] = time.time()
            self._persist()
            return dict(session)

    def sessions_in_states(self, states: set[str]) -> list[dict[str, Any]]:
        with self.lock:
            return [
                dict(session)
                for session in self._sessions.values()
                if session.get("status") in states
            ]