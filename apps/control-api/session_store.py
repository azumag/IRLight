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

# Sessions that still occupy a concurrent-session entitlement. STOPPING and
# FAILED_CLEANUP remain counted until provider resources have actually been
# reclaimed; allowing a replacement before cleanup completes can exceed the
# provider/resource cap even though the old stream is no longer LIVE.
CAPACITY_STATES = ACTIVE_STATES | {"STOPPING", "FAILED_CLEANUP"}

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


class EntitlementExceeded(RuntimeError):
    """Raised when a user has no free concurrent-session entitlement slot."""

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
        "node_id": None,
        "node_boot_id": None,
        "node_registered_at": None,
        "provisioning_started_at": None,
        "ready_at": None,
        "first_ingest_at": None,
        "last_ingest_at": None,
        "hold_deadline_at": None,
        "absolute_deadline_at": (
            now + absolute_deadline_hours * 3600 if absolute_deadline_hours else None
        ),
        "entitlement_id": None,
        "entitlement_reserved": False,
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

    def reserve_prepare_slot(
        self,
        session_id: str,
        *,
        user_id: str,
        environment: str,
        entitlement_id: str,
        max_concurrent_sessions: int,
    ) -> dict[str, Any]:
        """Atomically reserve one concurrent-session slot before prepare.

        The reservation flag closes the small race between creating a STOPPED
        session and transitioning it to PROVISIONING. Existing active sessions
        without the flag (state written by an older version) are still counted
        by status, so deploying this change does not accidentally grant an
        extra slot.
        """
        if max_concurrent_sessions < 1:
            raise EntitlementExceeded("concurrent session entitlement is disabled")

        with self.lock:
            existing = self._sessions.get(session_id)
            if existing is not None and existing.get("user_id") != user_id:
                raise KeyError(session_id)

            if existing is not None and existing.get("status") in {"FINISHED", "FAILED"}:
                return dict(existing)

            # Retrying prepare for a session that already occupies a slot must
            # not be treated as a new allocation, even if the user's plan was
            # downgraded after the session started.
            if existing is not None and (
                existing.get("entitlement_reserved")
                or existing.get("status") in CAPACITY_STATES
            ):
                existing["entitlement_id"] = entitlement_id
                existing["entitlement_reserved"] = True
                existing["updated_at"] = time.time()
                self._persist()
                return dict(existing)

            occupied = 0
            for other_id, session in self._sessions.items():
                if other_id == session_id or session.get("user_id") != user_id:
                    continue
                if session.get("entitlement_reserved") or session.get("status") in CAPACITY_STATES:
                    occupied += 1

            if occupied >= max_concurrent_sessions:
                raise EntitlementExceeded(
                    f"concurrent session limit exceeded ({occupied}/{max_concurrent_sessions})"
                )

            if existing is None:
                existing = new_session(
                    user_id=user_id,
                    environment=environment,
                    absolute_deadline_hours=12.0,
                )
                existing["session_id"] = session_id
                self._sessions[session_id] = existing

            existing["entitlement_id"] = entitlement_id
            existing["entitlement_reserved"] = True
            existing["updated_at"] = time.time()
            self._persist()
            return dict(existing)

    def get(self, session_id: str) -> dict[str, Any] | None:
        with self.lock:
            value = self._sessions.get(session_id)
            return dict(value) if value else None

    def list(self) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(value) for value in self._sessions.values()]

    def find_by_provider_server_id(
        self,
        provider_server_id: str,
        *,
        states: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        with self.lock:
            return [
                dict(session)
                for session in self._sessions.values()
                if session.get("provider_server_id") == provider_server_id
                and (states is None or session.get("status") in states)
            ]

    def update(self, session_id: str, **changes: Any) -> dict[str, Any]:
        with self.lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise KeyError(session_id)
            session.update(changes)
            session["updated_at"] = time.time()
            self._persist()
            return dict(session)

    def bind_node(
        self,
        session_id: str,
        *,
        node_id: str,
        boot_id: str,
        provider_server_id: str,
        registered_at: float | None = None,
    ) -> dict[str, Any]:
        current = time.time() if registered_at is None else registered_at
        with self.lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise KeyError(session_id)
            if session.get("provider_server_id") != provider_server_id:
                raise RuntimeError("node provider server does not match session")
            if session.get("status") not in ACTIVE_STATES:
                raise RuntimeError(f"session {session_id} is not active")
            session["node_id"] = node_id
            session["node_boot_id"] = boot_id
            session["node_registered_at"] = current
            session["updated_at"] = current
            self._persist()
            return dict(session)

    def apply_ingest_observation(
        self,
        session_id: str,
        *,
        node_id: str,
        event_types: list[str],
        observation: dict[str, Any],
        occurred_at: float | None = None,
    ) -> dict[str, Any]:
        """Atomically append Node ingest events and update Session lifecycle."""
        current = time.time() if occurred_at is None else occurred_at
        with self.lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise KeyError(session_id)
            if session.get("node_id") not in {None, node_id}:
                raise RuntimeError("node is not assigned to session")
            if session.get("status") not in ACTIVE_STATES:
                return dict(session)

            payload = {
                "node_id": node_id,
                "status": observation.get("status"),
                "path": observation.get("path"),
                "online": bool(observation.get("online", False)),
                "source_type": observation.get("source_type"),
                "source_id": observation.get("source_id"),
                "bitrate_bps": observation.get("bitrate_bps"),
                "max_bitrate_bps": observation.get("max_bitrate_bps"),
                "tracks": observation.get("tracks", []),
                "quality": observation.get("quality"),
                "reasons": observation.get("reasons", []),
                "warnings": observation.get("warnings", []),
                "enforced": observation.get("enforced", False),
                "observed_at": observation.get("observed_at"),
            }
            events = list(session.get("events", []))
            for event_type in event_types:
                reasons = payload.get("reasons") or []
                reason_code = (
                    str(reasons[0])[:100]
                    if event_type in {"ingest.rejected", "ingest.degraded"} and reasons
                    else None
                )
                events.append(
                    {
                        "sequence": len(events) + 1,
                        "type": event_type,
                        "reason_code": reason_code,
                        "payload": dict(payload),
                        "occurred_at": current,
                        "origin": "node-agent",
                    }
                )
            session["events"] = events

            online = bool(observation.get("online", False))
            ingest_status = str(observation.get("status", ""))
            state = str(session.get("status", ""))
            usable_online = online and ingest_status not in {"REJECTED", "OFFLINE", "UNKNOWN"}
            if usable_online and state in {"READY_WAIT_INGEST", "HOLDING"}:
                session["status"] = "LIVE"
                if session.get("first_ingest_at") is None:
                    session["first_ingest_at"] = current
                session["last_ingest_at"] = current
                session["hold_deadline_at"] = None
            elif not online and state == "LIVE":
                session["status"] = "HOLDING"
                session["last_ingest_at"] = current

            session["node_id"] = node_id
            session["updated_at"] = current
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
            if new_state in {"FINISHED", "FAILED"}:
                session["entitlement_reserved"] = False
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
