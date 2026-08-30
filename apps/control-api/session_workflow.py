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
import uuid
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

from ingest_store import default_ingest_store
from session_store import ProvisioningInProgress, SessionStore


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
        credential_store: Any | None = None,
    ) -> None:
        self.store = store
        self.provider = provider
        self.config = config or WorkflowConfig()
        self.credential_store = credential_store or default_ingest_store(store.state_dir)

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
        if current in ("READY_WAIT_INGEST", "LIVE", "DEGRADED", "HOLDING"):
            return session
        if current in ("FINISHED", "FAILED"):
            raise RuntimeError(f"session {session_id} is {current}")
        if current in ("STOPPING", "FAILED_CLEANUP"):
            raise RuntimeError(f"session {session_id} is {current}")

        operation_id = str(uuid.uuid4())
        claimed = False
        try:
            session = self.store.claim_provisioning(
                session_id,
                operation_id=operation_id,
                started_at=time.time(),
            )
            claimed = True
            volume = self._find_managed("volume", session_id)
            if volume is None:
                volume = self.provider.create_volume(
                    name=f"irlight-{session_id}-boot",
                    size_gb=self.config.size_gb,
                    metadata=self._metadata(user_id or "unknown", environment, session_id),
                )
            session = self.store.provisioning_checkpoint(
                session_id,
                operation_id=operation_id,
                provider_volume_id=volume.volume_id,
            )
            if self._provisioning_cancelled(session):
                return self._finish_cancelled_provisioning(session_id)

            server = self._find_managed("server", session_id)
            if server is None:
                server = self.provider.create_server(
                    name=f"irlight-{session_id}-node",
                    image_ref=self.config.image_ref,
                    flavor_ref=self.config.flavor_ref,
                    volume_id=str(volume.volume_id),
                    metadata=self._metadata(user_id or "unknown", environment, session_id),
                )
            session = self.store.provisioning_checkpoint(
                session_id,
                operation_id=operation_id,
                provider_server_id=server.server_id,
                provider_public_ipv4=server.public_ipv4,
            )
            if self._provisioning_cancelled(session):
                return self._finish_cancelled_provisioning(session_id)

            session = self.store.provisioning_checkpoint(
                session_id,
                operation_id=operation_id,
                next_state="BOOTSTRAPPING",
            )
            if self._provisioning_cancelled(session):
                return self._finish_cancelled_provisioning(session_id)
            # Production nodes must prove bootstrap and a healthy media stack
            # before becoming ingest-ready.  Dev/fake flows retain the
            # historical shortcut for local iteration.
            require_handshake = environment == "prod" and os.getenv(
                "IRLIGHT_REQUIRE_NODE_READY_HANDSHAKE", "1"
            ) != "0"
            if require_handshake:
                return session
            session = self.store.provisioning_checkpoint(
                session_id,
                operation_id=operation_id,
                next_state="READY_WAIT_INGEST",
                complete=True,
                ready_at=time.time(),
            )
            return session
        except ProvisioningInProgress:
            if claimed:
                current = self.store.get(session_id)
                if current is not None and self._provisioning_cancelled(current):
                    return self._finish_cancelled_provisioning(session_id)
            raise
        except Exception as exc:
            LOG.warning("provisioning failed for %s: %s", session_id, exc)
            current = self.store.get(session_id)
            if current is not None and self._provisioning_cancelled(current):
                return self._finish_cancelled_provisioning(session_id)
            failed = None
            try:
                failed = self.store.transition(
                    session_id,
                    "FAILED_CLEANUP",
                    allow_from={"PROVISIONING", "BOOTSTRAPPING"},
                    cleanup_pending=True,
                    failure_reason=str(exc)[:500],
                    provisioning_in_progress=False,
                    provisioning_operation_id=None,
                )
            except Exception:
                pass
            if failed is not None:
                try:
                    self.credential_store.revoke_session(session_id)
                except Exception:
                    pass
                lease_id = self.store.claim_session_cleanup(session_id)
                if lease_id is not None:
                    try:
                        if self._cleanup_resources(session_id, lease_id=lease_id):
                            try:
                                self.store.transition(
                                    session_id,
                                    "FAILED",
                                    allow_from={"FAILED_CLEANUP"},
                                    cleanup_pending=False,
                                )
                            except Exception:
                                pass
                    finally:
                        self.store.release_session_cleanup(session_id, lease_id)
            raise

    def stop(self, session_id: str) -> dict[str, Any]:
        """Move a Session toward STOPPING and clean provider resources."""
        session = self.store.get(session_id)
        if session is None:
            raise KeyError(session_id)
        current = str(session.get("status"))
        if current in ("STOPPED", "FINISHED", "FAILED"):
            # Reconcile credentials even when a prior worker crashed after a
            # terminal transition made the Session invisible to the reaper.
            self.credential_store.revoke_session(session_id)
            return session

        session = self.store.request_stop(session_id)
        # Credential authority is revoked before any provider cleanup or
        # terminal transition. A failed revoke leaves the Session retryable.
        self.credential_store.revoke_session(session_id)
        if bool(session.get("provisioning_in_progress")):
            return session
        lease_id = self.store.claim_session_cleanup(session_id)
        if lease_id is None:
            return self.store.get(session_id) or session
        try:
            if not self._cleanup_resources(session_id, lease_id=lease_id):
                return self.store.transition(
                    session_id,
                    "FAILED_CLEANUP",
                    allow_from={"STOPPING"},
                    cleanup_pending=True,
                    failure_reason="resource cleanup failed",
                    provisioning_in_progress=False,
                    provisioning_operation_id=None,
                )
            return self.store.transition(
                session_id,
                "FINISHED",
                allow_from={"STOPPING"},
                cleanup_pending=False,
                provisioning_in_progress=False,
                provisioning_operation_id=None,
                provisioning_cancel_requested=False,
            )
        finally:
            self.store.release_session_cleanup(session_id, lease_id)

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

        volume = self._find_managed("volume", session_id, user_id=user_id, environment=environment)
        if volume is None:
            volume = self.provider.create_volume(
                name=f"irlight-{session_id}-boot",
                size_gb=self.config.size_gb,
                metadata=tags,
            )

        server = self._find_managed("server", session_id, user_id=user_id, environment=environment)
        if server is None:
            server = self.provider.create_server(
                name=f"irlight-{session_id}-node",
                image_ref=self.config.image_ref,
                flavor_ref=self.config.flavor_ref,
                volume_id=str(volume.volume_id),
                metadata=tags,
            )
        return volume, server

    @staticmethod
    def _metadata(user_id: str, environment: str, session_id: str) -> dict[str, str]:
        return SessionMetadata(
            session_id=session_id,
            user_id=user_id,
            environment=environment,
        ).as_tags()

    @staticmethod
    def _provisioning_cancelled(session: dict[str, Any]) -> bool:
        return bool(session.get("provisioning_cancel_requested")) or str(
            session.get("status")
        ) in {"STOPPING", "FAILED_CLEANUP", "FINISHED", "FAILED"}

    def _finish_cancelled_provisioning(self, session_id: str) -> dict[str, Any]:
        current = self.store.get(session_id)
        if current is None:
            raise KeyError(session_id)
        status = str(current.get("status"))
        if status not in {"STOPPING", "FAILED_CLEANUP"}:
            return current
        self.credential_store.revoke_session(session_id)
        lease_id = self.store.claim_session_cleanup(session_id)
        if lease_id is None:
            return current
        try:
            cleaned = self._cleanup_resources(session_id, lease_id=lease_id)
            current = self.store.get(session_id)
            if current is None:
                raise KeyError(session_id)
            status = str(current.get("status"))
            common = {
                "provisioning_in_progress": False,
                "provisioning_operation_id": None,
                "provisioning_cancel_requested": False,
            }
            if status == "STOPPING":
                if cleaned:
                    return self.store.transition(
                        session_id,
                        "FINISHED",
                        allow_from={"STOPPING"},
                        cleanup_pending=False,
                        **common,
                    )
                return self.store.transition(
                    session_id,
                    "FAILED_CLEANUP",
                    allow_from={"STOPPING"},
                    cleanup_pending=True,
                    failure_reason="resource cleanup failed after provisioning cancellation",
                    **common,
                )
            if status == "FAILED_CLEANUP" and cleaned:
                return self.store.transition(
                    session_id,
                    "FAILED",
                    allow_from={"FAILED_CLEANUP"},
                    cleanup_pending=False,
                    **common,
                )
            if status == "FAILED_CLEANUP":
                return self.store.update(session_id, cleanup_pending=True, **common)
            return current
        finally:
            self.store.release_session_cleanup(session_id, lease_id)

    def _find_managed(
        self,
        kind: str,
        session_id: str,
        *,
        user_id: str | None = None,
        environment: str | None = None,
    ) -> Any | None:
        """Find an owned resource, rejecting same-session cross-owner reuse.

        Session IDs are client supplied, so they are not an ownership proof.
        A resource carrying the same ID but a different user/environment must
        never be adopted (or later deleted) by this workflow.
        """
        expected = {
            "irlight-session-id": session_id,
            **({"irlight-user-id": user_id} if user_id is not None else {}),
            **({"irlight-environment": environment} if environment is not None else {}),
        }
        resources = self.provider.list_volumes() if kind == "volume" else self.provider.list_servers()
        for resource in resources:
            tags = resource.tags
            if tags.get("irlight-session-id") != session_id:
                continue
            if tags.get("irlight-managed") != "true":
                continue
            if any(tags.get(key) != value for key, value in expected.items()):
                raise RuntimeError("managed resource ownership mismatch")
            return resource
        return None

    def _cleanup_resources(self, session_id: str, *, lease_id: str | None = None) -> bool:
        """Delete all provider resources for the session.

        Returns True when every delete succeeded (or there was nothing to
        delete). On failure the caller must stay in a non-terminal state so
        the reaper keeps retrying.
        """
        ok = True
        current = self.store.get(session_id) or {}
        expected_user = str(current.get("user_id", ""))
        expected_environment = str(current.get("environment", ""))
        resources = [
            resource
            for resource in self.provider.list_managed_resources()
            if resource.session_id == session_id
            and resource.user_id == expected_user
            and self._resource_environment(resource, expected_environment)
        ]
        servers = [r for r in resources if r.kind == "server" and r.session_id == session_id]
        for server in servers:
            if lease_id is not None and not self.store.session_cleanup_still_owned(
                session_id, lease_id
            ):
                return False
            try:
                self.provider.delete_server(str(server.provider_id))
            except Exception as exc:
                LOG.warning("server delete failed %s: %s", server.provider_id, exc)
                ok = False
        if not ok:
            return False
        remaining = [
            resource
            for resource in self.provider.list_managed_resources()
            if resource.session_id == session_id
            and resource.user_id == expected_user
            and self._resource_environment(resource, expected_environment)
        ]
        if any(
            resource.kind == "server" and resource.session_id == session_id
            for resource in remaining
        ):
            return False
        for volume in (
            resource
            for resource in remaining
            if resource.kind == "volume" and resource.session_id == session_id
        ):
            if lease_id is not None and not self.store.session_cleanup_still_owned(
                session_id, lease_id
            ):
                return False
            try:
                self.provider.delete_volume(str(volume.provider_id))
            except Exception as exc:
                LOG.warning("volume delete failed %s: %s", volume.provider_id, exc)
                ok = False
        return ok

    def _resource_environment(self, resource: Any, expected: str) -> bool:
        """Provider adapters expose tags on concrete resources, not inventory rows."""
        if not expected:
            return False
        for candidate in (*self.provider.list_servers(), *self.provider.list_volumes()):
            candidate_id = getattr(candidate, "provider_id", None) or getattr(candidate, "server_id", None) or getattr(candidate, "volume_id", None)
            if str(candidate_id or "") == str(resource.provider_id):
                return candidate.tags.get("irlight-environment") == expected
        return False


ACTIVE_OR_CLEANUP = {
    "PROVISIONING",
    "BOOTSTRAPPING",
    "READY_WAIT_INGEST",
    "LIVE",
    "DEGRADED",
    "HOLDING",
    "STOPPING",
    "FAILED_CLEANUP",
}
