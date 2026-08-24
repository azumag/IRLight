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

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from provider.conoha import SessionMetadata

from ingest_store import default_ingest_store
from session_store import ACTIVE_STATES, SessionStore


LOG = logging.getLogger("irlight.reaper")


@dataclass(frozen=True)
class ReaperConfig:
    provisioning_timeout_seconds: float = 600.0
    no_ingest_timeout_seconds: float = 3600.0
    hold_timeout_seconds: float = 1800.0
    heartbeat_grace_seconds: float = 120.0


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
        self.credential_store = default_ingest_store()

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
            with self.node_state_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
            LOG.warning("cannot read node state %s: %s", self.node_state_path, exc)
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("nodes"), dict):
            LOG.warning("invalid node state payload in %s", self.node_state_path)
            return None
        return payload

    @staticmethod
    def _heartbeat_baseline(
        session: dict[str, Any], node_state: dict[str, Any]
    ) -> tuple[float | None, float | None]:
        node_id = str(session.get("node_id") or "")
        if not node_id:
            return None, None

        node = node_state.get("nodes", {}).get(node_id)
        last_heartbeat: float | None = None
        if isinstance(node, dict) and str(node.get("session_id") or "") == str(
            session.get("session_id") or ""
        ):
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
                self._fail_and_cleanup(
                    session["session_id"],
                    "provisioning timeout",
                    reason_code="PROVISIONING_TIMEOUT",
                )
                result["timeout_failures"] += 1

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
                self._stop_and_cleanup(
                    session["session_id"], reason_code="NO_INGEST_TIMEOUT"
                )
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
                self.store.update(
                    str(session["session_id"]),
                    hold_deadline_at=hold_deadline,
                )
                # Newer SessionStore versions emit session.holding at the
                # LIVE/DEGRADED -> HOLDING transition itself. Older persisted
                # sessions can legitimately lack that audit. Recover the event
                # only when this hold interval has not already emitted one.
                if not self._has_holding_event_since(session, started):
                    self.store.append_event(
                        str(session["session_id"]),
                        event_type="session.holding",
                        reason_code="INGEST_DISCONNECTED",
                        payload={
                            "hold_started_at": started,
                            "hold_deadline_at": hold_deadline,
                        },
                        origin="reaper",
                        occurred_at=now,
                    )
                result["hold_deadlines_recovered"] += 1

            if now > float(hold_deadline):
                self._stop_and_cleanup(
                    session["session_id"], reason_code="HOLD_TIMEOUT"
                )
                result["deadline_stops"] += 1

        for session in self.store.sessions_in_states({"FAILED_CLEANUP"}):
            result["failed_cleanup_retries"] += 1
            if not self._cleanup_resources(session["session_id"]):
                continue
            self.credential_store.revoke_session(session["session_id"])
            failure_reason_code = str(
                session.get("failure_reason_code") or "RESOURCE_CLEANUP_FAILED"
            )[:100]
            self.store.transition(
                session["session_id"],
                "FAILED",
                allow_from={"FAILED_CLEANUP"},
                cleanup_pending=False,
            )
            self.store.append_event(
                session["session_id"],
                event_type="session.failed",
                reason_code=failure_reason_code,
                payload={
                    "failure_reason": session.get("failure_reason"),
                    "cleanup_pending": False,
                },
                origin="reaper",
                occurred_at=now,
            )

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
            self.store.transition(
                session_id,
                "FAILED_CLEANUP",
                allow_from=ACTIVE_STATES,
                cleanup_pending=True,
                failure_reason="node heartbeat timed out",
                failure_reason_code="NODE_SHUTDOWN",
            )
        except Exception as exc:
            LOG.warning("cannot fail stale node session %s: %s", session_id, exc)
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
        self.credential_store.revoke_session(session_id)
        return True

    def _fail_and_cleanup(
        self, session_id: str, reason: str, *, reason_code: str = "PIPELINE_CRASHED"
    ) -> None:
        try:
            self.store.transition(
                session_id,
                "FAILED_CLEANUP",
                allow_from={"PROVISIONING", "BOOTSTRAPPING"},
                cleanup_pending=True,
                failure_reason=reason,
                failure_reason_code=reason_code,
            )
        except Exception as exc:
            LOG.warning("cannot fail session %s: %s", session_id, exc)
            return
        if not self._cleanup_resources(session_id):
            return
        self.credential_store.revoke_session(session_id)
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

    def _stop_and_cleanup(
        self, session_id: str, *, reason_code: str = "DEADLINE_EXCEEDED"
    ) -> None:
        before = self.store.get(session_id)
        from_state = str(before.get("status")) if before else None
        try:
            self.store.transition(
                session_id,
                "STOPPING",
                allow_from={"READY_WAIT_INGEST", "LIVE", "HOLDING", "STOPPING"},
            )
        except Exception as exc:
            LOG.warning("cannot stop session %s: %s", session_id, exc)
            return
        if from_state != "STOPPING":
            self.store.append_event(
                session_id,
                event_type="session.stopping",
                reason_code=reason_code,
                payload={"from_state": from_state, "to_state": "STOPPING"},
                origin="reaper",
                occurred_at=self.now(),
            )
        if not self._cleanup_resources(session_id):
            self.store.transition(
                session_id,
                "FAILED_CLEANUP",
                allow_from={"STOPPING"},
                cleanup_pending=True,
                failure_reason="resource cleanup failed",
                failure_reason_code="RESOURCE_CLEANUP_FAILED",
            )
            return
        self.credential_store.revoke_session(session_id)
        self.store.transition(
            session_id,
            "FINISHED",
            allow_from={"STOPPING"},
            cleanup_pending=False,
        )
        self.store.append_event(
            session_id,
            event_type="session.finished",
            reason_code=reason_code,
            payload={"from_state": "STOPPING", "to_state": "FINISHED"},
            origin="reaper",
            occurred_at=self.now(),
        )

    def _cleanup_resources(self, session_id: str) -> bool:
        ok = True
        resources = self.provider.list_managed_resources()
        for resource in resources:
            if resource.session_id != session_id or resource.kind != "server":
                continue
            try:
                self.provider.delete_server(str(resource.provider_id))
            except Exception as exc:
                LOG.warning("cleanup failed %s %s: %s", resource.kind, resource.provider_id, exc)
                ok = False
        for resource in resources:
            if resource.session_id != session_id or resource.kind != "volume":
                continue
            try:
                self.provider.delete_volume(str(resource.provider_id))
            except Exception as exc:
                LOG.warning("cleanup failed %s %s: %s", resource.kind, resource.provider_id, exc)
                ok = False
        return ok

    def _cleanup_orphans(self) -> int:
        known = {str(s["session_id"]) for s in self.store.list()}
        cleaned = 0
        resources = self.provider.list_managed_resources()
        for resource in resources:
            session_id = resource.session_id
            if session_id is None or session_id in known or resource.kind != "server":
                continue
            try:
                self.provider.delete_server(str(resource.provider_id))
                cleaned += 1
            except Exception as exc:
                LOG.warning("orphan cleanup failed %s: %s", resource.provider_id, exc)
        for resource in resources:
            session_id = resource.session_id
            if session_id is None or session_id in known or resource.kind != "volume":
                continue
            try:
                self.provider.delete_volume(str(resource.provider_id))
                cleaned += 1
            except Exception as exc:
                LOG.warning("orphan cleanup failed %s: %s", resource.provider_id, exc)
        return cleaned
