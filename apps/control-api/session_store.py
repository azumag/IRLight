"""Persistent Session state and the Session state machine.

The state machine follows the Phase B design:

    STOPPED -> PROVISIONING -> BOOTSTRAPPING -> READY_WAIT_INGEST
             -> LIVE <-> DEGRADED -> HOLDING -> STOPPING -> FINISHED
                ^          ^           |
                +----------+-----------+

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
    "DEGRADED",
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
    "DEGRADED",
    "HOLDING",
}

TERMINAL_STATES = {"STOPPED", "FINISHED", "FAILED"}
CAPACITY_STATES = ACTIVE_STATES | {"STOPPING", "FAILED_CLEANUP"}
SESSION_EVENT_LIMIT = 1000
UNUSABLE_MEDIA_REASONS = {"VIDEO_TIMEOUT", "AUDIO_TIMEOUT"}
FORMAT_REJECTION_REASONS = {
    "VIDEO_CODEC_UNSUPPORTED",
    "AUDIO_CODEC_UNSUPPORTED",
    "RESOLUTION_UNSUPPORTED",
    "AUDIO_CHANNELS_UNSUPPORTED",
}

TRANSITIONS: dict[str, set[str]] = {
    "STOPPED": {"PROVISIONING"},
    "PROVISIONING": {"BOOTSTRAPPING", "STOPPING", "FAILED_CLEANUP"},
    "BOOTSTRAPPING": {"READY_WAIT_INGEST", "STOPPING", "FAILED_CLEANUP"},
    "READY_WAIT_INGEST": {"LIVE", "DEGRADED", "STOPPING", "FAILED_CLEANUP"},
    "LIVE": {"DEGRADED", "HOLDING", "STOPPING", "FAILED_CLEANUP"},
    "DEGRADED": {"LIVE", "HOLDING", "STOPPING", "FAILED_CLEANUP"},
    "HOLDING": {"LIVE", "DEGRADED", "STOPPING", "FAILED_CLEANUP"},
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
        "recovery_candidate_since": None,
        "recovery_candidate_source_id": None,
        "absolute_deadline_at": (
            now + absolute_deadline_hours * 3600 if absolute_deadline_hours else None
        ),
        "entitlement_id": None,
        "entitlement_reserved": False,
        "cleanup_pending": False,
        "failure_reason": None,
        "events": [],
        "next_event_seq": 1,
        "created_at": now,
        "updated_at": now,
    }


class SessionStore:
    """JSON-file persisted session store with an in-process lock."""

    def __init__(
        self,
        state_dir: str | os.PathLike[str] | None = None,
        *,
        recovery_stable_seconds: float | None = None,
    ) -> None:
        self.state_dir = Path(state_dir or os.getenv("STATE_DIR", "/state"))
        self.path = self.state_dir / "sessions.json"
        self.lock = threading.Lock()
        if recovery_stable_seconds is None:
            try:
                recovery_stable_seconds = float(
                    os.getenv("RECOVERY_STABLE_SECONDS", "3.0")
                )
            except ValueError:
                recovery_stable_seconds = 3.0
        self.recovery_stable_seconds = max(0.0, recovery_stable_seconds)
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

    @staticmethod
    def _next_event_sequence(session: dict[str, Any]) -> int:
        try:
            configured = int(session.get("next_event_seq", 0))
        except (TypeError, ValueError):
            configured = 0
        if configured > 0:
            return configured
        maximum = 0
        for event in session.get("events", []):
            if not isinstance(event, dict):
                continue
            try:
                maximum = max(maximum, int(event.get("sequence", 0)))
            except (TypeError, ValueError):
                continue
        return maximum + 1

    @staticmethod
    def _clear_recovery_candidate(session: dict[str, Any]) -> None:
        session["recovery_candidate_since"] = None
        session["recovery_candidate_source_id"] = None

    def _recovery_candidate_ready(
        self,
        session: dict[str, Any],
        *,
        source_id: object,
        current: float,
    ) -> bool:
        candidate_since_raw = session.get("recovery_candidate_since")
        candidate_source = session.get("recovery_candidate_source_id")
        try:
            candidate_since = (
                float(candidate_since_raw) if candidate_since_raw is not None else None
            )
        except (TypeError, ValueError):
            candidate_since = None

        if candidate_since is None or candidate_source != source_id or current < candidate_since:
            session["recovery_candidate_since"] = current
            session["recovery_candidate_source_id"] = source_id
            return self.recovery_stable_seconds <= 0.0

        return current - candidate_since >= self.recovery_stable_seconds

    def _append_event_locked(
        self,
        session: dict[str, Any],
        *,
        event_type: str,
        reason_code: str | None,
        payload: dict[str, Any],
        origin: str,
        occurred_at: float,
    ) -> dict[str, Any]:
        sequence = self._next_event_sequence(session)
        event = {
            "sequence": sequence,
            "type": event_type[:100],
            "reason_code": reason_code[:100] if reason_code else None,
            "payload": payload,
            "occurred_at": occurred_at,
            "origin": origin[:50],
        }
        events = list(session.get("events", []))
        events.append(event)
        session["events"] = events[-SESSION_EVENT_LIMIT:]
        session["next_event_seq"] = sequence + 1
        return dict(event)

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
        if max_concurrent_sessions < 1:
            raise EntitlementExceeded("concurrent session entitlement is disabled")

        with self.lock:
            existing = self._sessions.get(session_id)
            if existing is not None and existing.get("user_id") != user_id:
                raise KeyError(session_id)
            if existing is not None and existing.get("status") in {"FINISHED", "FAILED"}:
                return dict(existing)
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

    def append_event(
        self,
        session_id: str,
        *,
        event_type: str,
        reason_code: str | None = None,
        payload: dict[str, Any] | None = None,
        origin: str = "control-api",
        occurred_at: float | None = None,
    ) -> dict[str, Any]:
        current = time.time() if occurred_at is None else occurred_at
        with self.lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise KeyError(session_id)
            event = self._append_event_locked(
                session,
                event_type=event_type,
                reason_code=reason_code,
                payload=dict(payload or {}),
                origin=origin,
                occurred_at=current,
            )
            session["updated_at"] = current
            self._persist()
            return event

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
            for event_type in event_types:
                reasons = payload.get("reasons") or []
                reason_code = (
                    str(reasons[0])[:100]
                    if event_type in {"ingest.rejected", "ingest.degraded"} and reasons
                    else None
                )
                self._append_event_locked(
                    session,
                    event_type=event_type,
                    reason_code=reason_code,
                    payload=dict(payload),
                    origin="node-agent",
                    occurred_at=current,
                )

            online = bool(observation.get("online", False))
            ingest_status = str(observation.get("status", ""))
            state = str(session.get("status", ""))
            reasons = [str(reason)[:100] for reason in (payload.get("reasons") or [])]
            degraded_reason = reasons[0] if reasons else None
            unusable_reason = next(
                (reason for reason in reasons if reason in UNUSABLE_MEDIA_REASONS),
                None,
            )
            format_changed = any(reason in FORMAT_REJECTION_REASONS for reason in reasons)

            target_state: str | None = None
            lifecycle_event: str | None = None
            lifecycle_reason: str | None = None

            if state == "HOLDING":
                recovery_target: str | None = None
                recovery_reason: str | None = None
                if online and ingest_status in {"ACCEPTED", "WARNING"}:
                    recovery_target = "LIVE"
                elif online and ingest_status == "DEGRADED" and not unusable_reason:
                    recovery_target = "DEGRADED"
                    recovery_reason = degraded_reason

                if recovery_target is None:
                    self._clear_recovery_candidate(session)
                elif self._recovery_candidate_ready(
                    session,
                    source_id=payload.get("source_id"),
                    current=current,
                ):
                    target_state = recovery_target
                    lifecycle_event = "session.recovered"
                    lifecycle_reason = recovery_reason
            elif online and ingest_status == "DEGRADED" and state in {
                "READY_WAIT_INGEST",
                "LIVE",
                "DEGRADED",
            }:
                if unusable_reason and state in {"LIVE", "DEGRADED"}:
                    target_state = "HOLDING"
                    lifecycle_event = "session.holding"
                    lifecycle_reason = unusable_reason
                elif not unusable_reason:
                    target_state = "DEGRADED"
                    lifecycle_event = "session.degraded"
                    lifecycle_reason = degraded_reason
            elif online and ingest_status == "REJECTED" and state in {"LIVE", "DEGRADED"}:
                target_state = "HOLDING"
                lifecycle_event = "session.holding"
                lifecycle_reason = (
                    "FORMAT_CHANGED"
                    if format_changed
                    else degraded_reason or "INGEST_REJECTED"
                )
            elif online and ingest_status in {"ACCEPTED", "WARNING"} and state in {
                "READY_WAIT_INGEST",
                "DEGRADED",
            }:
                target_state = "LIVE"
                lifecycle_event = (
                    "session.live" if state == "READY_WAIT_INGEST" else "session.recovered"
                )
            elif not online and state in {"LIVE", "DEGRADED"}:
                target_state = "HOLDING"
                lifecycle_event = "session.holding"
                lifecycle_reason = "INGEST_DISCONNECTED"

            if target_state is not None and target_state != state:
                session["status"] = target_state
                self._clear_recovery_candidate(session)
                if target_state == "HOLDING":
                    session["last_ingest_at"] = current
                elif online:
                    if session.get("first_ingest_at") is None:
                        session["first_ingest_at"] = current
                    session["last_ingest_at"] = current
                    session["hold_deadline_at"] = None
                else:
                    session["last_ingest_at"] = current

                lifecycle_payload = dict(payload)
                lifecycle_payload["from_state"] = state
                lifecycle_payload["to_state"] = target_state
                assert lifecycle_event is not None
                self._append_event_locked(
                    session,
                    event_type=lifecycle_event,
                    reason_code=lifecycle_reason,
                    payload=lifecycle_payload,
                    origin="node-agent",
                    occurred_at=current,
                )
            elif state != "HOLDING":
                self._clear_recovery_candidate(session)

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
            self._clear_recovery_candidate(session)
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
