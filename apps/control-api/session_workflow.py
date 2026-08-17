"""Provisioning workflow for Session lifecycle.

The workflow drives provider resource creation for a Session:

    STOPPED -> PROVISIONING -> BOOTSTRAPPING -> READY_WAIT_INGEST

Provider access is isolated behind a small protocol so tests can inject the
in-memory fake and real ConoHa can be wired later. ``ensure_*`` is idempotent
per Session ID: retrying after a partial failure reuses existing resources
instead of creating duplicates.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

# Local checkouts run with cwd inside apps/control-api; add the repo root so
# `provider` imports resolve. In the container the Dockerfile sets
# PYTHONPATH=/app and provider/ is copied alongside, so this is skipped.
try:
    _LOCAL_REPO = Path(__file__).resolve().parents[2]
except IndexError:
    _LOCAL_REPO = None
if _LOCAL_REPO is not None and (_LOCAL_REPO / "provider").is_dir():
    if str(_LOCAL_REPO) not in sys.path:
        sys.path.insert(0, str(_LOCAL_REPO))

from provider.conoha import ProviderServer, ProviderVolume, SessionMetadata

from session_store import SessionStore


LOG = logging.getLogger("irlight.workflow")


class Provider(Protocol):
    def list_volumes(self) -> list[ProviderVolume]: ...

    def list_servers(self) -> list[ProviderServer]: ...

    def create_volume(
        self, name: str, size_gb: int, metadata: dict[str, str]
    ) -> ProviderVolume: ...

    def create_server(
        self,
        name: str,
        *,
        image_ref: str,
        flavor_ref: str,
        volume_id: str,
        metadata: dict[str, str],
    ) -> ProviderServer: ...

    def delete_volume(self, volume_id: str) -> None: ...

    def delete_server(self, server_id: str) -> None: ...

    def list_managed_resources(self) -> list[Any]: ...


@dataclass(frozen=True)
class WorkflowConfig:
    size_gb: int = 20
    image_ref: str = "ubuntu-24.04"
    flavor_ref: str = "g2"
    provisioning_timeout_seconds: float = 600.0


class ProvisioningWorkflow:
    def __init__(
        self,
        store: SessionStore,
        provider: Provider,
        config: WorkflowConfig | None = None,
    ) -> None:
        self.store = store
        self.provider = provider
        self.config = config or WorkflowConfig()

    # -- public API --------------------------------------------------------

    def prepare(
        self,
        session_id: str,
        *,
        user_id: str | None = None,
        environment: str = "dev",
    ) -> dict[str, Any]:
        """Ensure a Session is provisioned. Idempotent per Session ID."""
        session = self.store.get(session_id)
        if session is None:
            session = self.store.create(
                session_id=session_id,
                user_id=user_id or "unknown",
                environment=environment,
                absolute_deadline_hours=12.0,
            )

        current = str(session.get("status"))
        if current in ("READY_WAIT_INGEST", "LIVE", "HOLDING"):
            return session
        if current in ("FINISHED", "FAILED"):
            raise RuntimeError(f"session {session_id} is {current}")
        if current in ("STOPPING", "FAILED_CLEANUP"):
            raise RuntimeError(f"session {session_id} is {current}")

        try:
            session = self.store.transition(
                session_id,
                "PROVISIONING",
                allow_from={"STOPPED", "PROVISIONING", "BOOTSTRAPPING"},
                provisioning_started_at=time.time(),
            )
            volume, server = self._ensure_resources(session_id, user_id or "unknown", environment)
            session = self.store.transition(
                session_id,
                "BOOTSTRAPPING",
                allow_from={"PROVISIONING", "BOOTSTRAPPING"},
                provider_volume_id=volume.volume_id,
                provider_server_id=server.server_id,
                provider_public_ipv4=server.public_ipv4,
            )
            # In the spike the Node Agent registration is out of scope; the
            # session becomes ingest-ready once provider resources are up.
            session = self.store.transition(
                session_id,
                "READY_WAIT_INGEST",
                allow_from={"BOOTSTRAPPING"},
                ready_at=time.time(),
            )
            return session
        except Exception as exc:
            LOG.warning("provisioning failed for %s: %s", session_id, exc)
            try:
                self.store.transition(
                    session_id,
                    "FAILED_CLEANUP",
                    allow_from={"PROVISIONING", "BOOTSTRAPPING"},
                    cleanup_pending=True,
                    failure_reason=str(exc)[:500],
                )
            except Exception:
                pass
            raise

    def stop(self, session_id: str) -> dict[str, Any]:
        """Move a Session toward STOPPING and clean provider resources."""
        session = self.store.get(session_id)
        if session is None:
            raise KeyError(session_id)
        current = str(session.get("status"))
        if current in ("STOPPED", "FINISHED"):
            return session
        if current == "FAILED":
            return session

        session = self.store.transition(
            session_id,
            "STOPPING",
            allow_from=ACTIVE_OR_CLEANUP,
        )
        if not self._cleanup_resources(session_id):
            return self.store.transition(
                session_id,
                "FAILED_CLEANUP",
                allow_from={"STOPPING"},
                cleanup_pending=True,
                failure_reason="resource cleanup failed",
            )
        return self.store.transition(
            session_id,
            "FINISHED",
            allow_from={"STOPPING"},
            cleanup_pending=False,
        )

    # -- internals ----------------------------------------------------------

    def _ensure_resources(
        self, session_id: str, user_id: str, environment: str
    ) -> tuple[ProviderVolume, ProviderServer]:
        metadata = SessionMetadata(
            session_id=session_id,
            user_id=user_id,
            environment=environment,
        )
        tags = metadata.as_tags()

        volume = self._find_managed("volume", session_id)
        if volume is None:
            volume = self.provider.create_volume(
                name=f"irlight-{session_id}-boot",
                size_gb=self.config.size_gb,
                metadata=tags,
            )

        server = self._find_managed("server", session_id)
        if server is None:
            server = self.provider.create_server(
                name=f"irlight-{session_id}-node",
                image_ref=self.config.image_ref,
                flavor_ref=self.config.flavor_ref,
                volume_id=str(volume.volume_id),
                metadata=tags,
            )
        return volume, server

    def _find_managed(self, kind: str, session_id: str) -> Any | None:
        if kind == "volume":
            for volume in self.provider.list_volumes():
                if volume.tags.get("irlight-session-id") == session_id:
                    return volume
        else:
            for server in self.provider.list_servers():
                if server.tags.get("irlight-session-id") == session_id:
                    return server
        return None

    def _cleanup_resources(self, session_id: str) -> bool:
        """Delete all provider resources for the session.

        Returns True when every delete succeeded (or there was nothing to
        delete). On failure the caller must stay in a non-terminal state so
        the reaper keeps retrying.
        """
        ok = True
        resources = self.provider.list_managed_resources()
        servers = [r for r in resources if r.kind == "server" and r.session_id == session_id]
        volumes = [r for r in resources if r.kind == "volume" and r.session_id == session_id]
        for server in servers:
            try:
                self.provider.delete_server(str(server.provider_id))
            except Exception as exc:
                LOG.warning("server delete failed %s: %s", server.provider_id, exc)
                ok = False
        for volume in volumes:
            try:
                self.provider.delete_volume(str(volume.provider_id))
            except Exception as exc:
                LOG.warning("volume delete failed %s: %s", volume.provider_id, exc)
                ok = False
        return ok


ACTIVE_OR_CLEANUP = {
    "PROVISIONING",
    "BOOTSTRAPPING",
    "READY_WAIT_INGEST",
    "LIVE",
    "HOLDING",
    "STOPPING",
    "FAILED_CLEANUP",
}