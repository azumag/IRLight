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
import math
import os
import tempfile
import threading
import time
import uuid
import fcntl
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from state_safety import mark_initialized, was_initialized


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


class SessionStateError(RuntimeError):
    """Raised when persisted Session state cannot be trusted."""

    pass


class ProvisioningInProgress(RuntimeError):
    """Raised when another worker owns the Session provisioning lease."""

    pass


class OrphanCleanupInProgress(RuntimeError):
    """Raised when provider cleanup owns a Session ID tombstone."""

    pass


def _reject_non_finite_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value} is not allowed")


def _require_nonempty_string(
    record: dict[str, Any], field: str, *, context: str
) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value:
        raise SessionStateError(f"{context} has invalid {field}")
    return value


def _require_finite_number(
    record: dict[str, Any], field: str, *, context: str, optional: bool = False
) -> float | None:
    value = record.get(field)
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SessionStateError(f"{context} has invalid {field}")
    try:
        number = float(value)
    except (OverflowError, ValueError):
        raise SessionStateError(f"{context} has invalid {field}") from None
    if not math.isfinite(number):
        raise SessionStateError(f"{context} has invalid {field}")
    return number


def _require_bool(
    record: dict[str, Any], field: str, *, context: str, optional: bool = False
) -> bool | None:
    if field not in record and optional:
        return None
    value = record.get(field)
    if not isinstance(value, bool):
        raise SessionStateError(f"{context} has invalid {field}")
    return value


def _require_nonnegative_int(
    record: dict[str, Any], field: str, *, context: str, optional: bool = False
) -> int | None:
    if field not in record and optional:
        return None
    value = record.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SessionStateError(f"{context} has invalid {field}")
    return value


def new_session_id() -> str:
    return str(uuid.uuid4())


def new_session(
    *,
    user_id: str,
    environment: str = "dev",
    egress_mode: str = "DIRECT_PUSH",
    absolute_deadline_hours: float | None = None,
) -> dict[str, Any]:
    now = time.time()
    return {
        "session_id": new_session_id(),
        "user_id": user_id,
        "environment": environment,
        "egress_mode": egress_mode,
        "status": "STOPPED",
        "relay_client_status": None,
        "relay_client_connected": False,
        "relay_client_reader_count": 0,
        "relay_client_last_reason": None,
        "relay_client_updated_at": None,
        "idempotency_key": None,
        "prepare_request": None,
        "version": 0,
        "destination_id": None,
        "provider": None,
        "provider_volume_id": None,
        "provider_server_id": None,
        "provider_public_ipv4": None,
        "node_id": None,
        "node_boot_id": None,
        "node_registered_at": None,
        "node_last_heartbeat_at": None,
        "provisioning_started_at": None,
        "provisioning_operation_id": None,
        "provisioning_in_progress": False,
        "provisioning_cancel_requested": False,
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
    """JSON-file persisted session store with process-safe transactions."""

    def __init__(
        self,
        state_dir: str | os.PathLike[str] | None = None,
        *,
        recovery_stable_seconds: float | None = None,
    ) -> None:
        self.state_dir = Path(state_dir or os.getenv("STATE_DIR", "/state"))
        self.path = self.state_dir / "sessions.json"
        self.lock_path = self.state_dir / ".sessions.lock"
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
        self._orphan_cleanup_leases: dict[str, dict[str, Any]] = {}
        self._authoritative = False
        with self._state_lock(exclusive=False):
            pass

    @contextmanager
    def _state_lock(self, *, exclusive: bool):
        """Reload state while holding both thread and cross-process locks."""
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
            except SessionStateError:
                raise
            except OSError as exc:
                raise SessionStateError(
                    f"cannot lock Session state {self.path}: {exc}"
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
                raise SessionStateError(
                    f"Session state {self.path} disappeared after initialization"
                )
            self._sessions = {}
            self._orphan_cleanup_leases = {}
            self._authoritative = False
            return
        except (ValueError, OSError) as exc:
            raise SessionStateError(
                f"cannot read Session state {self.path}: {exc}"
            ) from exc

        if not isinstance(raw, dict) or not isinstance(raw.get("sessions"), dict):
            raise SessionStateError(f"invalid Session state payload in {self.path}")
        sessions = raw["sessions"]
        self._validate_sessions(sessions)
        leases = raw.get("orphan_cleanup_leases", {})
        if not isinstance(leases, dict):
            raise SessionStateError(f"invalid cleanup lease state in {self.path}")
        self._validate_cleanup_leases(leases)
        self._sessions = dict(sessions)
        self._orphan_cleanup_leases = dict(leases)
        mark_initialized(self.path)
        self._authoritative = True

    @staticmethod
    def _validate_sessions(sessions: dict[Any, Any]) -> None:
        required_timestamps = ("created_at", "updated_at")
        optional_timestamps = (
            "relay_client_updated_at",
            "node_registered_at",
            "node_last_heartbeat_at",
            "provisioning_started_at",
            "ready_at",
            "first_ingest_at",
            "last_ingest_at",
            "hold_deadline_at",
            "recovery_candidate_since",
            "absolute_deadline_at",
        )
        optional_booleans = (
            "entitlement_reserved",
            "provisioning_in_progress",
            "provisioning_cancel_requested",
            "relay_client_connected",
        )

        for session_id, record in sessions.items():
            if (
                not isinstance(session_id, str)
                or not session_id
                or not isinstance(record, dict)
            ):
                raise SessionStateError("invalid Session state record")

            stored_id = _require_nonempty_string(
                record, "session_id", context="Session state record"
            )
            _require_nonempty_string(record, "user_id", context="Session state record")
            status = _require_nonempty_string(
                record, "status", context="Session state record"
            )
            if stored_id != session_id:
                raise SessionStateError(
                    "Session state record session_id does not match its key"
                )
            if status not in SESSION_STATES:
                raise SessionStateError("Session state record has unknown status")

            _require_nonnegative_int(
                record, "version", context="Session state record"
            )
            _require_bool(record, "cleanup_pending", context="Session state record")
            for field in required_timestamps:
                _require_finite_number(record, field, context="Session state record")
            for field in optional_timestamps:
                if field in record:
                    _require_finite_number(
                        record,
                        field,
                        context="Session state record",
                        optional=True,
                    )
            for field in optional_booleans:
                if field in record:
                    _require_bool(
                        record, field, context="Session state record", optional=True
                    )
            if "relay_client_reader_count" in record:
                _require_nonnegative_int(
                    record,
                    "relay_client_reader_count",
                    context="Session state record",
                    optional=True,
                )
            next_event_seq: int | None = None
            if "next_event_seq" in record:
                next_event_seq = _require_nonnegative_int(
                    record,
                    "next_event_seq",
                    context="Session state record",
                    optional=True,
                )
                if next_event_seq == 0:
                    raise SessionStateError(
                        "Session state record has invalid next_event_seq"
                    )

            events = record.get("events", [])
            if not isinstance(events, list):
                raise SessionStateError("Session state record has invalid events")
            previous_event_sequence = 0
            for event in events:
                if not isinstance(event, dict):
                    raise SessionStateError("Session state record has invalid event")
                event_sequence = _require_nonnegative_int(
                    event, "sequence", context="Session event"
                )
                if event_sequence == 0 or event_sequence <= previous_event_sequence:
                    raise SessionStateError("Session event sequence is not strictly increasing")
                previous_event_sequence = event_sequence

                _require_nonempty_string(event, "type", context="Session event")
                _require_nonempty_string(event, "origin", context="Session event")
                _require_finite_number(event, "occurred_at", context="Session event")

                reason_code = event.get("reason_code")
                if reason_code is not None and not isinstance(reason_code, str):
                    raise SessionStateError("Session event has invalid reason_code")
                if not isinstance(event.get("payload"), dict):
                    raise SessionStateError("Session event has invalid payload")

            if (
                next_event_seq is not None
                and previous_event_sequence > 0
                and next_event_seq <= previous_event_sequence
            ):
                raise SessionStateError(
                    "Session state record next_event_seq does not advance retained events"
                )

    @staticmethod
    def _validate_cleanup_leases(leases: dict[Any, Any]) -> None:
        for session_id, lease in leases.items():
            if (
                not isinstance(session_id, str)
                or not session_id
                or not isinstance(lease, dict)
            ):
                raise SessionStateError("invalid cleanup lease state record")
            _require_nonempty_string(lease, "lease_id", context="cleanup lease record")
            scope = _require_nonempty_string(
                lease, "scope", context="cleanup lease record"
            )
            if scope not in {"orphan", "session"}:
                raise SessionStateError("cleanup lease record has unknown scope")
            created_at = _require_finite_number(
                lease, "created_at", context="cleanup lease record"
            )
            expires_at = _require_finite_number(
                lease, "expires_at", context="cleanup lease record"
            )
            assert created_at is not None and expires_at is not None
            if expires_at <= created_at:
                raise SessionStateError("cleanup lease record has invalid expiry")

            if scope == "orphan":
                _require_nonempty_string(
                    lease, "resource_id", context="cleanup lease record"
                )
                _require_nonempty_string(
                    lease, "resource_kind", context="cleanup lease record"
                )
                continue

            expected_states = lease.get("expected_states")
            if (
                not isinstance(expected_states, list)
                or not expected_states
                or any(
                    not isinstance(state, str) or state not in SESSION_STATES
                    for state in expected_states
                )
            ):
                raise SessionStateError(
                    "cleanup lease record has invalid expected_states"
                )

    def _persist(self) -> None:
        self._validate_sessions(self._sessions)
        self._validate_cleanup_leases(self._orphan_cleanup_leases)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.state_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                try:
                    json.dump(
                        {
                            "sessions": self._sessions,
                            "orphan_cleanup_leases": self._orphan_cleanup_leases,
                        },
                        handle,
                        ensure_ascii=False,
                        sort_keys=True,
                        allow_nan=False,
                    )
                except (TypeError, ValueError) as exc:
                    raise SessionStateError(
                        "Session state cannot be serialized"
                    ) from exc
                handle.flush()
                os.fsync(handle.fileno())
            # A missing sessions.json must never look like a pristine store
            # after an attempted authoritative commit. Arm the durable fuse
            # before publishing the payload, matching state_safety's contract.
            mark_initialized(self.path)
            os.replace(temporary, self.path)
            directory_fd = os.open(self.state_dir, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            self._authoritative = True
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
        with self._state_lock(exclusive=True):
            session = new_session(**kwargs)
            if session_id is not None:
                self._reject_active_cleanup_lease_locked(session_id)
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

        with self._state_lock(exclusive=True):
            self._reject_active_cleanup_lease_locked(session_id)
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

    def begin_prepare(
        self,
        session_id: str,
        *,
        user_id: str,
        environment: str,
        entitlement_id: str,
        max_concurrent_sessions: int,
        destination_id: str | None,
        egress_mode: str,
        idempotency_key: str,
    ) -> tuple[dict[str, Any], bool]:
        """Atomically bind a prepare request, configuration and capacity slot.

        Returns ``(session, replay)``. Once bound, neither a concurrent request
        nor a later Idempotency-Key can mutate the Session's transport target.
        """
        if max_concurrent_sessions < 1:
            raise EntitlementExceeded("concurrent session entitlement is disabled")
        request = {
            "environment": environment,
            "destination_id": destination_id,
            "egress_mode": egress_mode,
        }
        with self._state_lock(exclusive=True):
            self._reject_active_cleanup_lease_locked(session_id)
            existing = self._sessions.get(session_id)
            if existing is not None and existing.get("user_id") != user_id:
                raise KeyError(session_id)
            if existing is not None and existing.get("status") in {"FINISHED", "FAILED"}:
                raise InvalidTransition(
                    f"session {session_id} is {existing.get('status')}"
                )

            if existing is not None and existing.get("idempotency_key") is not None:
                configured = existing.get("prepare_request")
                if not isinstance(configured, dict):
                    configured = {
                        "environment": existing.get("environment"),
                        "destination_id": existing.get("destination_id"),
                        "egress_mode": existing.get("egress_mode", "DIRECT_PUSH"),
                    }
                if configured != request:
                    if configured.get("egress_mode") != egress_mode:
                        raise InvalidTransition("egress mode cannot be changed")
                    if configured.get("destination_id") != destination_id:
                        raise InvalidTransition("destination cannot be changed")
                    raise InvalidTransition("environment cannot be changed")
                if existing.get("idempotency_key") != idempotency_key:
                    raise InvalidTransition(
                        "session is already bound to another Idempotency-Key"
                    )
                return dict(existing), True

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
                    egress_mode=egress_mode,
                    absolute_deadline_hours=12.0,
                )
                existing["session_id"] = session_id
                self._sessions[session_id] = existing
            existing.update(
                {
                    "environment": environment,
                    "destination_id": destination_id,
                    "egress_mode": egress_mode,
                    "prepare_request": request,
                    "idempotency_key": idempotency_key,
                    "entitlement_id": entitlement_id,
                    "entitlement_reserved": True,
                    "updated_at": time.time(),
                }
            )
            self._persist()
            return dict(existing), False

    def get_prepare_replay(
        self,
        session_id: str,
        *,
        user_id: str,
        environment: str,
        destination_id: str | None,
        egress_mode: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        """Return an exact committed replay without external revalidation."""
        request = {
            "environment": environment,
            "destination_id": destination_id,
            "egress_mode": egress_mode,
        }
        with self._state_lock(exclusive=False):
            existing = self._sessions.get(session_id)
            if existing is None:
                return None
            if existing.get("user_id") != user_id:
                raise KeyError(session_id)
            if existing.get("idempotency_key") != idempotency_key:
                return None
            configured = existing.get("prepare_request")
            if not isinstance(configured, dict):
                configured = {
                    "environment": existing.get("environment"),
                    "destination_id": existing.get("destination_id"),
                    "egress_mode": existing.get("egress_mode", "DIRECT_PUSH"),
                }
            if configured != request:
                raise InvalidTransition(
                    "Idempotency-Key was already used with different prepare parameters"
                )
            return dict(existing)

    def get(self, session_id: str) -> dict[str, Any] | None:
        with self._state_lock(exclusive=False):
            value = self._sessions.get(session_id)
            return dict(value) if value else None

    def list(self) -> list[dict[str, Any]]:
        with self._state_lock(exclusive=False):
            return [dict(value) for value in self._sessions.values()]

    def authoritative_snapshot(self) -> list[dict[str, Any]]:
        """Return one trusted snapshot or refuse provider-wide decisions."""
        with self._state_lock(exclusive=False):
            if not self._authoritative:
                raise SessionStateError(
                    f"Session state {self.path} is missing or uninitialized"
                )
            return [dict(value) for value in self._sessions.values()]

    def find_by_provider_server_id(
        self,
        provider_server_id: str,
        *,
        states: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        with self._state_lock(exclusive=False):
            return [
                dict(session)
                for session in self._sessions.values()
                if session.get("provider_server_id") == provider_server_id
                and (states is None or session.get("status") in states)
            ]

    def update(self, session_id: str, **changes: Any) -> dict[str, Any]:
        with self._state_lock(exclusive=True):
            session = self._sessions.get(session_id)
            if session is None:
                raise KeyError(session_id)
            session.update(changes)
            session["updated_at"] = time.time()
            self._persist()
            return dict(session)

    def claim_provisioning(
        self,
        session_id: str,
        *,
        operation_id: str,
        started_at: float,
    ) -> dict[str, Any]:
        """Atomically acquire the single provisioning lease for a Session."""
        with self._state_lock(exclusive=True):
            self._reject_active_cleanup_lease_locked(session_id)
            session = self._sessions.get(session_id)
            if session is None:
                raise KeyError(session_id)
            current = str(session.get("status"))
            existing = session.get("provisioning_operation_id")
            if current not in {"STOPPED", "PROVISIONING", "BOOTSTRAPPING"}:
                raise InvalidTransition(f"cannot provision Session in {current}")
            if existing and existing != operation_id:
                raise ProvisioningInProgress(
                    f"session {session_id} is already being provisioned"
                )
            session.update(
                {
                    "status": "PROVISIONING",
                    "provisioning_operation_id": operation_id,
                    "provisioning_in_progress": True,
                    "provisioning_cancel_requested": False,
                    "provisioning_started_at": started_at,
                    "cleanup_pending": False,
                    "updated_at": time.time(),
                }
            )
            self._persist()
            return dict(session)

    def _active_cleanup_lease_locked(
        self, session_id: str, *, current: float | None = None
    ) -> dict[str, Any] | None:
        lease = self._orphan_cleanup_leases.get(session_id)
        if lease is None:
            return None
        now = time.time() if current is None else current
        try:
            expires_at = float(lease.get("expires_at", 0))
        except (TypeError, ValueError):
            expires_at = 0
        if expires_at <= now:
            return None
        return lease

    def _reject_active_cleanup_lease_locked(self, session_id: str) -> None:
        if self._active_cleanup_lease_locked(session_id) is not None:
            raise OrphanCleanupInProgress(
                f"provider cleanup is in progress for Session {session_id}"
            )

    @staticmethod
    def _matches_fields(
        session: dict[str, Any], expected_fields: dict[str, Any] | None
    ) -> bool:
        return all(
            session.get(field) == expected
            for field, expected in (expected_fields or {}).items()
        )

    def claim_orphan_cleanup(
        self,
        session_id: str,
        *,
        resource_id: str,
        resource_kind: str,
        lease_seconds: float = 900.0,
    ) -> str | None:
        """Tombstone a Session ID so provisioning cannot race a provider delete."""
        with self._state_lock(exclusive=True):
            session = self._sessions.get(session_id)
            if session is not None and str(session.get("status")) not in TERMINAL_STATES:
                return None
            if self._active_cleanup_lease_locked(session_id) is not None:
                return None
            lease_id = str(uuid.uuid4())
            now = time.time()
            self._orphan_cleanup_leases[session_id] = {
                "lease_id": lease_id,
                "scope": "orphan",
                "resource_id": resource_id,
                "resource_kind": resource_kind,
                "created_at": now,
                "expires_at": now + max(1.0, lease_seconds),
            }
            self._persist()
            return lease_id

    def orphan_cleanup_still_owned(self, session_id: str, lease_id: str) -> bool:
        with self._state_lock(exclusive=False):
            session = self._sessions.get(session_id)
            lease = self._active_cleanup_lease_locked(session_id)
            return bool(
                lease is not None
                and lease.get("lease_id") == lease_id
                and lease.get("scope", "orphan") == "orphan"
                and (session is None or str(session.get("status")) in TERMINAL_STATES)
            )

    def release_orphan_cleanup(self, session_id: str, lease_id: str) -> None:
        with self._state_lock(exclusive=True):
            lease = self._orphan_cleanup_leases.get(session_id)
            if lease is None or lease.get("lease_id") != lease_id:
                return
            self._orphan_cleanup_leases.pop(session_id, None)
            self._persist()

    def claim_session_cleanup(
        self,
        session_id: str,
        *,
        expected_states: set[str] | None = None,
        lease_seconds: float = 900.0,
    ) -> str | None:
        """Serialize destructive cleanup for one non-terminal Session."""
        allowed = expected_states or {"STOPPING", "FAILED_CLEANUP"}
        with self._state_lock(exclusive=True):
            session = self._sessions.get(session_id)
            if session is None or str(session.get("status")) not in allowed:
                return None
            if self._active_cleanup_lease_locked(session_id) is not None:
                return None
            lease_id = str(uuid.uuid4())
            now = time.time()
            self._orphan_cleanup_leases[session_id] = {
                "lease_id": lease_id,
                "scope": "session",
                "expected_states": sorted(allowed),
                "created_at": now,
                "expires_at": now + max(1.0, lease_seconds),
            }
            self._persist()
            return lease_id

    def session_cleanup_still_owned(self, session_id: str, lease_id: str) -> bool:
        with self._state_lock(exclusive=False):
            session = self._sessions.get(session_id)
            lease = self._active_cleanup_lease_locked(session_id)
            expected_states = set(lease.get("expected_states", [])) if lease else set()
            return bool(
                session is not None
                and lease is not None
                and lease.get("lease_id") == lease_id
                and lease.get("scope") == "session"
                and str(session.get("status")) in expected_states
            )

    def release_session_cleanup(self, session_id: str, lease_id: str) -> None:
        with self._state_lock(exclusive=True):
            lease = self._orphan_cleanup_leases.get(session_id)
            if (
                lease is None
                or lease.get("lease_id") != lease_id
                or lease.get("scope") != "session"
            ):
                return
            self._orphan_cleanup_leases.pop(session_id, None)
            self._persist()

    def provisioning_checkpoint(
        self,
        session_id: str,
        *,
        operation_id: str,
        next_state: str | None = None,
        complete: bool = False,
        **changes: Any,
    ) -> dict[str, Any]:
        """Persist provider IDs and advance only while the lease is owned."""
        with self._state_lock(exclusive=True):
            session = self._sessions.get(session_id)
            if session is None:
                raise KeyError(session_id)
            if session.get("provisioning_operation_id") != operation_id:
                raise ProvisioningInProgress(
                    f"provisioning lease for {session_id} is no longer owned"
                )

            session.update(changes)
            current = str(session.get("status"))
            cancelled = bool(session.get("provisioning_cancel_requested")) or current in {
                "STOPPING",
                "FAILED_CLEANUP",
                "FINISHED",
                "FAILED",
            }
            if next_state is not None and not cancelled:
                allowed = TRANSITIONS.get(current, set())
                if next_state not in allowed:
                    raise InvalidTransition(f"{current} -> {next_state} not allowed")
                session["status"] = next_state
                self._clear_recovery_candidate(session)
            if complete and not cancelled:
                session["provisioning_operation_id"] = None
                session["provisioning_in_progress"] = False
                session["provisioning_cancel_requested"] = False
            session["updated_at"] = time.time()
            self._persist()
            return dict(session)

    def request_stop(self, session_id: str) -> dict[str, Any]:
        """Atomically request cancellation before any resource cleanup."""
        with self._state_lock(exclusive=True):
            session = self._sessions.get(session_id)
            if session is None:
                raise KeyError(session_id)
            current = str(session.get("status"))
            if current in TERMINAL_STATES:
                return dict(session)
            if current not in ACTIVE_STATES | {"STOPPING", "FAILED_CLEANUP"}:
                raise InvalidTransition(f"cannot stop Session in {current}")
            session.update(
                {
                    "status": "STOPPING",
                    "provisioning_cancel_requested": True,
                    "cleanup_pending": True,
                    "updated_at": time.time(),
                }
            )
            self._clear_recovery_candidate(session)
            self._persist()
            return dict(session)

    def request_stop_if_current(
        self,
        session_id: str,
        *,
        expected_status: str,
        expected_fields: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Request STOPPING only if the reaper snapshot is still current."""
        with self._state_lock(exclusive=True):
            session = self._sessions.get(session_id)
            if (
                session is None
                or str(session.get("status")) != expected_status
                or not self._matches_fields(session, expected_fields)
            ):
                return None
            session.update(
                {
                    "status": "STOPPING",
                    "provisioning_cancel_requested": True,
                    "cleanup_pending": True,
                    "updated_at": time.time(),
                }
            )
            self._clear_recovery_candidate(session)
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
        with self._state_lock(exclusive=True):
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
        with self._state_lock(exclusive=True):
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
            session["node_last_heartbeat_at"] = None
            session["updated_at"] = current
            self._persist()
            return dict(session)

    def record_node_heartbeat(
        self,
        session_id: str,
        *,
        node_id: str,
        boot_id: str,
        observed_at: float,
        node_ready: bool = False,
    ) -> bool:
        """Persist the heartbeat generation used by reaper CAS decisions."""
        with self._state_lock(exclusive=True):
            session = self._sessions.get(session_id)
            if (
                session is None
                or session.get("status") not in ACTIVE_STATES
                or session.get("node_id") != node_id
                or session.get("node_boot_id") != boot_id
            ):
                return False
            previous = session.get("node_last_heartbeat_at")
            try:
                if previous is not None and float(previous) > float(observed_at):
                    return False
            except (TypeError, ValueError):
                return False
            session["node_last_heartbeat_at"] = float(observed_at)
            if node_ready and session.get("status") == "BOOTSTRAPPING":
                session["status"] = "READY_WAIT_INGEST"
                session["ready_at"] = float(observed_at)
            # Do not touch updated_at: HOLDING timeout recovery uses it only
            # as a legacy interval baseline.
            self._persist()
            return True

    def update_if_current(
        self,
        session_id: str,
        *,
        expected_status: str,
        expected_fields: dict[str, Any],
        event: dict[str, Any] | None = None,
        **changes: Any,
    ) -> dict[str, Any] | None:
        """Apply an update (and optional audit event) against one snapshot."""
        with self._state_lock(exclusive=True):
            session = self._sessions.get(session_id)
            if (
                session is None
                or str(session.get("status")) != expected_status
                or not self._matches_fields(session, expected_fields)
            ):
                return None
            current = time.time()
            session.update(changes)
            if event is not None:
                occurred_at = float(event.get("occurred_at", current))
                self._append_event_locked(
                    session,
                    event_type=str(event.get("event_type", "session.updated")),
                    reason_code=(
                        str(event["reason_code"])
                        if event.get("reason_code") is not None
                        else None
                    ),
                    payload=dict(event.get("payload", {})),
                    origin=str(event.get("origin", "control-api")),
                    occurred_at=occurred_at,
                )
                current = occurred_at
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
        with self._state_lock(exclusive=True):
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

    def apply_relay_client_observation(
        self,
        session_id: str,
        *,
        node_id: str,
        event_types: list[str],
        observation: dict[str, Any],
    ) -> dict[str, Any]:
        """Audit relay client changes and persist the safe aggregate state."""
        current = time.time()
        with self._state_lock(exclusive=True):
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
                "connected": bool(observation.get("connected", False)),
                "reader_count": max(0, int(observation.get("reader_count", 0) or 0)),
                "reason_code": observation.get("reason_code"),
                "observed_at": observation.get("observed_at"),
            }
            for event_type in event_types:
                self._append_event_locked(
                    session,
                    event_type=event_type,
                    reason_code=(
                        str(payload["reason_code"])[:100]
                        if payload["reason_code"]
                        else None
                    ),
                    payload=payload,
                    origin="node-agent",
                    occurred_at=current,
                )
            session.update(
                {
                    "relay_client_status": payload["status"],
                    "relay_client_connected": payload["connected"],
                    "relay_client_reader_count": payload["reader_count"],
                    "relay_client_last_reason": payload["reason_code"],
                    "relay_client_updated_at": payload["observed_at"],
                    "node_id": node_id,
                    "updated_at": current,
                }
            )
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
        with self._state_lock(exclusive=True):
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

    def transition_if_current(
        self,
        session_id: str,
        new_state: str,
        *,
        allow_from: set[str],
        expected_fields: dict[str, Any],
        **changes: Any,
    ) -> dict[str, Any] | None:
        """Transition only if all fields from a reaper snapshot still match."""
        if new_state not in SESSION_STATES:
            raise InvalidTransition(f"unknown state: {new_state}")
        with self._state_lock(exclusive=True):
            session = self._sessions.get(session_id)
            if (
                session is None
                or str(session.get("status")) not in allow_from
                or not self._matches_fields(session, expected_fields)
            ):
                return None
            session.update(changes)
            session["status"] = new_state
            self._clear_recovery_candidate(session)
            if new_state in {"FINISHED", "FAILED"}:
                session["entitlement_reserved"] = False
            session["updated_at"] = time.time()
            self._persist()
            return dict(session)

    def sessions_in_states(self, states: set[str]) -> list[dict[str, Any]]:
        with self._state_lock(exclusive=False):
            return [
                dict(session)
                for session in self._sessions.values()
                if session.get("status") in states
            ]

    @property
    def is_authoritative(self) -> bool:
        """Whether an existing, valid persisted state file was loaded."""
        with self._state_lock(exclusive=False):
            return self._authoritative
