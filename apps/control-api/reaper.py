"""Scheduled reaper for Session lifecycle.

The reaper owns provider resource cleanup independently from the workflow:

- sessions stuck in PROVISIONING/BOOTSTRAPPING past the provisioning timeout
- sessions in READY_WAIT_INGEST / LIVE / HOLDING past their deadlines
- provider resources whose Session ID no longer exists in the store
- provider resources left behind after FAILED_CLEANUP / STOPPING

It is safe to run repeatedly; every delete re-checks provider inventory.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from provider.conoha import SessionMetadata

from session_store import SessionStore


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
    ) -> None:
        self.store = store
        self.provider = provider
        self.config = config or ReaperConfig()
        self._now = now

    def now(self) -> float:
        return self._now if self._now is not None else time.time()

    def run(self) -> dict[str, Any]:
        """One sweep; returns counts for tests and logs."""
        result = {
            "timeout_failures": 0,
            "deadline_stops": 0,
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
                self._fail_and_cleanup(session["session_id"], "provisioning timeout")
                result["timeout_failures"] += 1

        for session in self.store.sessions_in_states({"READY_WAIT_INGEST"}):
            ready = session.get("ready_at")
            ready = float(ready) if ready is not None else now
            if now - ready > self.config.no_ingest_timeout_seconds:
                self._stop_and_cleanup(session["session_id"])
                result["deadline_stops"] += 1

        for session in self.store.sessions_in_states({"HOLDING"}):
            hold_deadline = session.get("hold_deadline_at")
            if hold_deadline is not None and now > float(hold_deadline):
                self._stop_and_cleanup(session["session_id"])
                result["deadline_stops"] += 1

        for session in self.store.sessions_in_states({"FAILED_CLEANUP"}):
            result["failed_cleanup_retries"] += 1
            if not self._cleanup_resources(session["session_id"]):
                continue
            self.store.transition(
                session["session_id"],
                "FAILED",
                allow_from={"FAILED_CLEANUP"},
                cleanup_pending=False,
            )

        result["orphan_cleanup"] = self._cleanup_orphans()
        return result

    # -- internals ----------------------------------------------------------

    def _fail_and_cleanup(self, session_id: str, reason: str) -> None:
        try:
            self.store.transition(
                session_id,
                "FAILED_CLEANUP",
                allow_from={"PROVISIONING", "BOOTSTRAPPING"},
                cleanup_pending=True,
                failure_reason=reason,
            )
        except Exception as exc:
            LOG.warning("cannot fail session %s: %s", session_id, exc)
            return
        if not self._cleanup_resources(session_id):
            return
        self.store.transition(
            session_id,
            "FAILED",
            allow_from={"FAILED_CLEANUP"},
            cleanup_pending=False,
        )

    def _stop_and_cleanup(self, session_id: str) -> None:
        try:
            self.store.transition(
                session_id,
                "STOPPING",
                allow_from={"READY_WAIT_INGEST", "LIVE", "HOLDING", "STOPPING"},
            )
        except Exception as exc:
            LOG.warning("cannot stop session %s: %s", session_id, exc)
            return
        if not self._cleanup_resources(session_id):
            self.store.transition(
                session_id,
                "FAILED_CLEANUP",
                allow_from={"STOPPING"},
                cleanup_pending=True,
                failure_reason="resource cleanup failed",
            )
            return
        self.store.transition(
            session_id,
            "FINISHED",
            allow_from={"STOPPING"},
            cleanup_pending=False,
        )

    def _cleanup_resources(self, session_id: str) -> bool:
        """Delete provider resources for the session; False on any failure."""
        ok = True
        resources = self.provider.list_managed_resources()
        # Delete servers first: volumes are attached to servers and deletion
        # fails while the attachment exists.
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
        """Delete provider resources that reference unknown sessions."""
        known = {str(s["session_id"]) for s in self.store.list()}
        cleaned = 0
        resources = self.provider.list_managed_resources()
        for resource in resources:
            session_id = resource.session_id
            if (
                session_id is None
                or session_id in known
                or resource.kind != "server"
            ):
                continue
            try:
                self.provider.delete_server(str(resource.provider_id))
                cleaned += 1
            except Exception as exc:
                LOG.warning("orphan cleanup failed %s: %s", resource.provider_id, exc)
        for resource in resources:
            session_id = resource.session_id
            if (
                session_id is None
                or session_id in known
                or resource.kind != "volume"
            ):
                continue
            try:
                self.provider.delete_volume(str(resource.provider_id))
                cleaned += 1
            except Exception as exc:
                LOG.warning("orphan cleanup failed %s: %s", resource.provider_id, exc)
        return cleaned