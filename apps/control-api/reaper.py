"""Scheduled reaper for Session lifecycle.

The reaper owns provider resource cleanup independently from the workflow:

- sessions stuck in PROVISIONING/BOOTSTRAPPING past the provisioning timeout
- sessions whose assigned Node Agent heartbeat disappears past its grace period
- sessions in READY_WAIT_INGEST / LIVE / HOLDING past their deadlines
- provider resources whose Session ID no longer exists in the store
- provider resources left behind after FAILED_CLEANUP / STOPPING

It is safe to run repeatedly; every delete re-checks provider inventory.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from provider.conoha import SessionMetadata

from ingest_store import default_ingest_store
from node_internal import NodeStateError, read_node_authority_snapshot
from session_store import (
    ACTIVE_STATES,
    TERMINAL_STATES,
    SessionStateError,
    SessionStore,
)


LOG = logging.getLogger("irlight.reaper")


@dataclass(frozen=True)
class ReaperConfig:
    provisioning_timeout_seconds: float = 600.0
    no_ingest_timeout_seconds: float = 3600.0
    hold_timeout_seconds: float = 1800.0
    heartbeat_grace_seconds: float = 120.0
    orphan_grace_seconds: float = 300.0


class Reaper:
    def __init__(
        self,
        store: SessionStore,
        provider: Any,
        config: ReaperConfig | None = None,
        *,
        now: float | None = None,
        node_state_path: Path | None = None,
    ) -> None:
        self.store = store
        self.provider = provider
        self.config = config or ReaperConfig()
        self._now = now
        self.node_state_path = node_state_path or Path(
            os.getenv("NODE_STATE_DIR", "/state")
        ) / "nodes.json"
        # Reaper and Control API must operate on the same durable credential
        # file even when STATE_DIR was supplied directly to SessionStore.
        self.credential_store = default_ingest_store(store.state_dir)

    def now(self) -> float:
        return self._now if self._now is not None else time.time()

    @staticmethod
    def _has_holding_event_since(session: dict[str, Any], started: float) -> bool:
        """Return whether this HOLDING interval is already lifecycle-audited."""
        for event in reversed(session.get("events", [])):
            if not isinstance(event, dict) or event.get("type") != "session.holding":
                continue
            try:
                occurred_at = float(event.get("occurred_at"))
            except (TypeError, ValueError):
                continue
            if occurred_at >= started:
                return True
        return False

    def _read_node_state(self) -> dict[str, Any] | None:
        """Read the Node registry once per sweep.

        A missing/corrupt registry is not evidence that every Node disappeared.
        In that case this sweep skips heartbeat enforcement instead of tearing
        down otherwise healthy Sessions.
        """
        try:
            return read_node_authority_snapshot(self.node_state_path)
        except (NodeStateError, OSError) as exc:
            LOG.warning("cannot trust node state %s: %s", self.node_state_path, exc)
            return None

    @staticmethod
    def _heartbeat_baseline(
        session: dict[str, Any], node_state: dict[str, Any]
    ) -> tuple[float | None, float | None]:
        node_id = str(session.get("node_id") or "")
        if not node_id:
            return None, None

        last_heartbeat: float | None = None
        try:
            raw_session_last = session.get("node_last_heartbeat_at")
            if raw_session_last is not None:
                last_heartbeat = float(raw_session_last)
        except (TypeError, ValueError):
            last_heartbeat = None

        node = node_state.get("nodes", {}).get(node_id)
        if last_heartbeat is None and isinstance(node, dict) and str(
            node.get("session_id") or ""
        ) == str(session.get("session_id") or ""):
            try:
                raw_last = node.get("last_heartbeat_at")
                if raw_last is not None:
                    last_heartbeat = float(raw_last)
            except (TypeError, ValueError):
                last_heartbeat = None

        registered_at: float | None = None
        try:
            raw_registered = session.get("node_registered_at")
            if raw_registered is not None:
                registered_at = float(raw_registered)
        except (TypeError, ValueError):
            registered_at = None

        return last_heartbeat or registered_at, last_heartbeat

    def run(self) -> dict[str, Any]:
        """One sweep; returns counts for tests and logs."""
        result = {
            "timeout_failures": 0,
            "heartbeat_failures": 0,
            "deadline_stops": 0,
            "hold_deadlines_recovered": 0,
            "orphan_cleanup": 0,
            "failed_cleanup_retries": 0,
        }
        now = self.now()

        for session in self.store.sessions_in_states({"PROVISIONING", "BOOTSTRAPPING"}):
            started = session.get("provisioning_started_at")
            if started is None:
                started = session.get("created_at")
            if started is None:
                started = now
            started = float(started)
            if now - started > self.config.provisioning_timeout_seconds:
                if self._fail_and_cleanup(
                    session["session_id"],
                    "provisioning timeout",
                    reason_code="PROVISIONING_TIMEOUT",
                    expected_status=str(session.get("status")),
                    expected_fields={
                        "provisioning_started_at": session.get("provisioning_started_at"),
                        "provisioning_operation_id": session.get("provisioning_operation_id"),
                    },
                ):
                    result["timeout_failures"] += 1

        for session in self.store.sessions_in_states(ACTIVE_STATES):
            deadline = session.get("absolute_deadline_at")
            if deadline is None:
                continue
            try:
                expired = now >= float(deadline)
            except (TypeError, ValueError):
                LOG.warning(
                    "invalid absolute deadline for Session %s",
                    session.get("session_id"),
                )
                continue
            if expired:
                if self._stop_and_cleanup(
                    str(session["session_id"]),
                    reason_code="ABSOLUTE_DEADLINE_EXCEEDED",
                    expected_status=str(session.get("status")),
                    expected_fields={"absolute_deadline_at": deadline},
                ):
                    result["deadline_stops"] += 1

        node_state = self._read_node_state()
        if node_state is not None:
            grace = max(0.0, float(self.config.heartbeat_grace_seconds))
            for session in self.store.sessions_in_states(ACTIVE_STATES):
                baseline, last_heartbeat = self._heartbeat_baseline(session, node_state)
                if baseline is None or now < baseline or now - baseline < grace:
                    continue
                if self._fail_missing_heartbeat(
                    session,
                    last_heartbeat_at=last_heartbeat,
                    detected_at=now,
                ):
                    result["heartbeat_failures"] += 1

        for session in self.store.sessions_in_states({"READY_WAIT_INGEST"}):
            ready = session.get("ready_at")
            ready = float(ready) if ready is not None else now
            if now - ready > self.config.no_ingest_timeout_seconds:
                if self._stop_and_cleanup(
                    session["session_id"],
                    reason_code="NO_INGEST_TIMEOUT",
                    expected_status="READY_WAIT_INGEST",
                    expected_fields={"ready_at": session.get("ready_at")},
                ):
                    result["deadline_stops"] += 1

        for session in self.store.sessions_in_states({"HOLDING"}):
            hold_deadline = session.get("hold_deadline_at")
            if hold_deadline is None:
                started = session.get("last_ingest_at")
                if started is None:
                    started = session.get("updated_at")
                if started is None:
                    started = now
                started = float(started)
                hold_deadline = started + self.config.hold_timeout_seconds
                event = None
                if not self._has_holding_event_since(session, started):
                    event = {
                        "event_type": "session.holding",
                        "reason_code": "INGEST_DISCONNECTED",
                        "payload": {
                            "hold_started_at": started,
                            "hold_deadline_at": hold_deadline,
                        },
                        "origin": "reaper",
                        "occurred_at": now,
                    }
                recovered = self.store.update_if_current(
                    str(session["session_id"]),
                    expected_status="HOLDING",
                    expected_fields={
                        "hold_deadline_at": None,
                        "last_ingest_at": session.get("last_ingest_at"),
                        "updated_at": session.get("updated_at"),
                    },
                    event=event,
                    hold_deadline_at=hold_deadline,
                )
                if recovered is None:
                    continue
                session = recovered
                result["hold_deadlines_recovered"] += 1

            if now > float(hold_deadline):
                if self._stop_and_cleanup(
                    session["session_id"],
                    reason_code="HOLD_TIMEOUT",
                    expected_status="HOLDING",
                    expected_fields={
                        "hold_deadline_at": hold_deadline,
                        "last_ingest_at": session.get("last_ingest_at"),
                    },
                ):
                    result["deadline_stops"] += 1

        for session in self.store.sessions_in_states({"STOPPING"}):
            self._stop_and_cleanup(
                str(session["session_id"]),
                reason_code="STOP_REQUESTED",
            )

        for session in self.store.sessions_in_states({"FAILED_CLEANUP"}):
            result["failed_cleanup_retries"] += 1
            session_id = str(session["session_id"])
            try:
                self.credential_store.revoke_session(session_id)
            except Exception as exc:
                LOG.warning("cannot revoke credentials for %s: %s", session_id, exc)
                continue
            lease_id = self.store.claim_session_cleanup(session_id)
            if lease_id is None:
                continue
            try:
                if not self._cleanup_resources(session_id, lease_id=lease_id):
                    continue
                failure_reason_code = str(
                    session.get("failure_reason_code") or "RESOURCE_CLEANUP_FAILED"
                )[:100]
                self.store.transition(
                    session_id,
                    "FAILED",
                    allow_from={"FAILED_CLEANUP"},
                    cleanup_pending=False,
                )
                self.store.append_event(
                    session_id,
                    event_type="session.failed",
                    reason_code=failure_reason_code,
                    payload={
                        "failure_reason": session.get("failure_reason"),
                        "cleanup_pending": False,
                    },
                    origin="reaper",
                    occurred_at=now,
                )
            finally:
                self.store.release_session_cleanup(session_id, lease_id)

        result["orphan_cleanup"] = self._cleanup_orphans()
        return result

    def _fail_missing_heartbeat(
        self,
        session: dict[str, Any],
        *,
        last_heartbeat_at: float | None,
        detected_at: float,
    ) -> bool:
        session_id = str(session["session_id"])
        from_state = str(session.get("status", ""))
        try:
            transitioned = self.store.transition_if_current(
                session_id,
                "FAILED_CLEANUP",
                allow_from=ACTIVE_STATES,
                expected_fields={
                    "node_id": session.get("node_id"),
                    "node_boot_id": session.get("node_boot_id"),
                    "node_registered_at": session.get("node_registered_at"),
                    "node_last_heartbeat_at": session.get("node_last_heartbeat_at"),
                },
                cleanup_pending=True,
                failure_reason="node heartbeat timed out",
                failure_reason_code="NODE_SHUTDOWN",
            )
        except Exception as exc:
            LOG.warning("cannot fail stale node session %s: %s", session_id, exc)
            return False
        if transitioned is None:
            return False
        self.store.append_event(
            session_id,
            event_type="session.failure_detected",
            reason_code="NODE_SHUTDOWN",
            payload={
                "node_id": session.get("node_id"),
                "from_state": from_state,
                "to_state": "FAILED_CLEANUP",
                "cleanup_pending": True,
                "last_heartbeat_at": last_heartbeat_at,
                "node_registered_at": session.get("node_registered_at"),
                "heartbeat_grace_seconds": max(
                    0.0, float(self.config.heartbeat_grace_seconds)
                ),
            },
            origin="reaper",
            occurred_at=detected_at,
        )
        try:
            self.credential_store.revoke_session(session_id)
        except Exception as exc:
            LOG.warning("cannot revoke credentials for %s: %s", session_id, exc)
            return False
        return True

    def _fail_and_cleanup(
        self,
        session_id: str,
        reason: str,
        *,
        reason_code: str = "PIPELINE_CRASHED",
        expected_status: str | None = None,
        expected_fields: dict[str, Any] | None = None,
    ) -> bool:
        try:
            if expected_status is None:
                failed = self.store.transition(
                    session_id,
                    "FAILED_CLEANUP",
                    allow_from={"PROVISIONING", "BOOTSTRAPPING"},
                    cleanup_pending=True,
                    failure_reason=reason,
                    failure_reason_code=reason_code,
                    provisioning_cancel_requested=True,
                    provisioning_in_progress=False,
                    provisioning_operation_id=None,
                )
            else:
                failed = self.store.transition_if_current(
                    session_id,
                    "FAILED_CLEANUP",
                    allow_from={expected_status},
                    expected_fields=expected_fields or {},
                    cleanup_pending=True,
                    failure_reason=reason,
                    failure_reason_code=reason_code,
                    provisioning_cancel_requested=True,
                    provisioning_in_progress=False,
                    provisioning_operation_id=None,
                )
        except Exception as exc:
            LOG.warning("cannot fail session %s: %s", session_id, exc)
            return False
        if failed is None:
            return False
        try:
            self.credential_store.revoke_session(session_id)
        except Exception as exc:
            LOG.warning("cannot revoke credentials for %s: %s", session_id, exc)
            return True
        lease_id = self.store.claim_session_cleanup(session_id)
        if lease_id is None:
            return True
        try:
            if not self._cleanup_resources(session_id, lease_id=lease_id):
                return True
            self.store.transition(
                session_id,
                "FAILED",
                allow_from={"FAILED_CLEANUP"},
                cleanup_pending=False,
            )
            self.store.append_event(
                session_id,
                event_type="session.failed",
                reason_code=reason_code,
                payload={"failure_reason": reason, "cleanup_pending": False},
                origin="reaper",
                occurred_at=self.now(),
            )
        finally:
            self.store.release_session_cleanup(session_id, lease_id)
        return True

    def _stop_and_cleanup(
        self,
        session_id: str,
        *,
        reason_code: str = "DEADLINE_EXCEEDED",
        expected_status: str | None = None,
        expected_fields: dict[str, Any] | None = None,
    ) -> bool:
        before = self.store.get(session_id) if expected_status is None else None
        from_state = expected_status or (str(before.get("status")) if before else None)
        try:
            if expected_status is None:
                stopping = self.store.request_stop(session_id)
            else:
                stopping = self.store.request_stop_if_current(
                    session_id,
                    expected_status=expected_status,
                    expected_fields=expected_fields,
                )
        except Exception as exc:
            LOG.warning("cannot stop session %s: %s", session_id, exc)
            return False
        if stopping is None:
            return False
        if str(stopping.get("status")) in TERMINAL_STATES:
            return False
        try:
            self.credential_store.revoke_session(session_id)
        except Exception as exc:
            LOG.warning("cannot revoke credentials for %s: %s", session_id, exc)
            return True
        if from_state != "STOPPING":
            self.store.append_event(
                session_id,
                event_type="session.stopping",
                reason_code=reason_code,
                payload={"from_state": from_state, "to_state": "STOPPING"},
                origin="reaper",
                occurred_at=self.now(),
            )
        if bool(stopping.get("provisioning_in_progress")):
            try:
                started = float(
                    stopping.get("provisioning_started_at")
                    or stopping.get("updated_at")
                    or self.now()
                )
            except (TypeError, ValueError):
                started = self.now()
            if self.now() - started <= self.config.provisioning_timeout_seconds:
                return True
            self.store.update(
                session_id,
                provisioning_in_progress=False,
                provisioning_operation_id=None,
            )
        lease_id = self.store.claim_session_cleanup(session_id)
        if lease_id is None:
            return True
        try:
            if not self._cleanup_resources(session_id, lease_id=lease_id):
                self.store.transition(
                    session_id,
                    "FAILED_CLEANUP",
                    allow_from={"STOPPING"},
                    cleanup_pending=True,
                    failure_reason="resource cleanup failed",
                    failure_reason_code="RESOURCE_CLEANUP_FAILED",
                    provisioning_in_progress=False,
                    provisioning_operation_id=None,
                )
                return True
            self.store.transition(
                session_id,
                "FINISHED",
                allow_from={"STOPPING"},
                cleanup_pending=False,
                provisioning_in_progress=False,
                provisioning_operation_id=None,
                provisioning_cancel_requested=False,
            )
            self.store.append_event(
                session_id,
                event_type="session.finished",
                reason_code=reason_code,
                payload={"from_state": "STOPPING", "to_state": "FINISHED"},
                origin="reaper",
                occurred_at=self.now(),
            )
        finally:
            self.store.release_session_cleanup(session_id, lease_id)
        return True

    def _cleanup_resources(self, session_id: str, *, lease_id: str | None = None) -> bool:
        ok = True
        resources = self.provider.list_managed_resources()
        for resource in resources:
            if resource.session_id != session_id or resource.kind != "server":
                continue
            if lease_id is not None and not self.store.session_cleanup_still_owned(
                session_id, lease_id
            ):
                return False
            try:
                self.provider.delete_server(str(resource.provider_id))
            except Exception as exc:
                LOG.warning("cleanup failed %s %s: %s", resource.kind, resource.provider_id, exc)
                ok = False
        if not ok:
            return False
        remaining = self.provider.list_managed_resources()
        if any(
            resource.session_id == session_id and resource.kind == "server"
            for resource in remaining
        ):
            LOG.warning("cleanup deferred volumes for %s: server still attached", session_id)
            return False
        for resource in remaining:
            if resource.session_id != session_id or resource.kind != "volume":
                continue
            if lease_id is not None and not self.store.session_cleanup_still_owned(
                session_id, lease_id
            ):
                return False
            try:
                self.provider.delete_volume(str(resource.provider_id))
            except Exception as exc:
                LOG.warning("cleanup failed %s %s: %s", resource.kind, resource.provider_id, exc)
                ok = False
        return ok

    def _cleanup_orphans(self) -> int:
        try:
            sessions = self.store.authoritative_snapshot()
        except SessionStateError as exc:
            LOG.warning("skipping orphan cleanup: %s", exc)
            return 0
        known = {str(s["session_id"]): s for s in sessions}
        cleaned = 0
        resources = self.provider.list_managed_resources()
        for resource in resources:
            session_id = resource.session_id
            if (
                resource.kind != "server"
                or not self._orphan_delete_allowed(resource, known)
            ):
                continue
            if self._cleanup_orphan_resource(resource):
                cleaned += 1
        for resource in resources:
            session_id = resource.session_id
            if (
                resource.kind != "volume"
                or not self._orphan_delete_allowed(resource, known)
            ):
                continue
            if self._cleanup_orphan_resource(resource):
                cleaned += 1
        return cleaned

    def _cleanup_orphan_resource(self, resource: Any) -> bool:
        session_id = str(resource.session_id)
        try:
            lease_id = self.store.claim_orphan_cleanup(
                session_id,
                resource_id=str(resource.provider_id),
                resource_kind=str(resource.kind),
            )
        except SessionStateError as exc:
            LOG.warning("orphan cleanup lease failed %s: %s", resource.provider_id, exc)
            return False
        if lease_id is None:
            return False
        try:
            latest_resources = self.provider.list_managed_resources()
            current = next(
                (
                    candidate
                    for candidate in latest_resources
                    if str(candidate.provider_id) == str(resource.provider_id)
                    and candidate.kind == resource.kind
                    and str(candidate.session_id) == session_id
                ),
                None,
            )
            if current is None or not self.store.orphan_cleanup_still_owned(
                session_id, lease_id
            ):
                return False
            if resource.kind == "volume" and any(
                str(candidate.session_id) == session_id and candidate.kind == "server"
                for candidate in latest_resources
            ):
                return False
            if resource.kind == "server":
                self.provider.delete_server(str(resource.provider_id))
            elif resource.kind == "volume":
                self.provider.delete_volume(str(resource.provider_id))
            else:
                return False
            return True
        except Exception as exc:
            LOG.warning("orphan cleanup failed %s: %s", resource.provider_id, exc)
            return False
        finally:
            try:
                self.store.release_orphan_cleanup(session_id, lease_id)
            except SessionStateError as exc:
                LOG.warning(
                    "orphan cleanup lease release failed %s: %s",
                    resource.provider_id,
                    exc,
                )

    def _orphan_delete_allowed(
        self,
        resource: Any,
        known: dict[str, dict[str, Any]],
    ) -> bool:
        session_id = resource.session_id
        if session_id is None:
            return False
        session = known.get(str(session_id))
        if session is not None and str(session.get("status")) not in TERMINAL_STATES:
            return False
        now = self.now()
        try:
            if resource.delete_after is not None and now >= float(resource.delete_after):
                return True
        except (TypeError, ValueError):
            return False
        try:
            created_at = float(resource.created_at)
        except (TypeError, ValueError):
            return False
        return now - created_at >= max(0.0, self.config.orphan_grace_seconds)

    def _orphan_still_unowned(self, session_id: str) -> bool:
        """Recheck state immediately before each destructive provider call."""
        try:
            latest = {
                str(session["session_id"]): session
                for session in self.store.authoritative_snapshot()
            }
        except SessionStateError as exc:
            LOG.warning("aborting orphan cleanup recheck: %s", exc)
            return False
        session = latest.get(session_id)
        return session is None or str(session.get("status")) in TERMINAL_STATES
