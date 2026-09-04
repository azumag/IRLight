from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "provider"))
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from fake_provider import FakeProvider  # noqa: E402
from reaper import Reaper, ReaperConfig  # noqa: E402
from session_store import (  # noqa: E402
    InvalidTransition,
    OrphanCleanupInProgress,
    ProvisioningInProgress,
    SessionStore,
)
from session_workflow import ProvisioningWorkflow, WorkflowConfig  # noqa: E402


def make_store() -> SessionStore:
    return SessionStore(tempfile.mkdtemp(prefix="irlight-sessions-"))


class FailingDeleteProvider(FakeProvider):
    """FakeProvider that fails deletes until failures are cleared."""

    def __init__(self, fail_server: bool = False, fail_volume: bool = False) -> None:
        super().__init__()
        self.fail_server = fail_server
        self.fail_volume = fail_volume

    def delete_server(self, server_id: str) -> None:
        if self.fail_server:
            raise RuntimeError("server delete failed (injected)")
        super().delete_server(server_id)

    def delete_volume(self, volume_id: str) -> None:
        if self.fail_volume:
            raise RuntimeError("volume delete failed (injected)")
        super().delete_volume(volume_id)


class BlockingCreateProvider(FakeProvider):
    def __init__(self) -> None:
        super().__init__()
        self.create_started = threading.Event()
        self.release_create = threading.Event()
        self.volume_create_count = 0
        self.server_create_count = 0

    def create_volume(self, name, size_gb, metadata):
        self.volume_create_count += 1
        self.create_started.set()
        if not self.release_create.wait(timeout=5):
            raise RuntimeError("timed out waiting to resume provider create")
        return super().create_volume(name, size_gb, metadata)

    def create_server(self, name, **kwargs):
        self.server_create_count += 1
        return super().create_server(name, **kwargs)


class ListHookProvider(FakeProvider):
    """Run one state mutation between the orphan snapshot and its recheck."""

    def __init__(self) -> None:
        super().__init__()
        self.on_next_list = None

    def list_managed_resources(self):
        callback = self.on_next_list
        self.on_next_list = None
        if callback is not None:
            callback()
        return super().list_managed_resources()


class DeleteHookProvider(FakeProvider):
    def __init__(self) -> None:
        super().__init__()
        self.on_delete_server = None

    def delete_server(self, server_id: str) -> None:
        callback = self.on_delete_server
        self.on_delete_server = None
        if callback is not None:
            callback()
        super().delete_server(server_id)


class WorkflowPrepareTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = make_store()
        self.provider = FakeProvider()
        self.workflow = ProvisioningWorkflow(
            self.store, self.provider, WorkflowConfig()
        )
        self.session_id = str(uuid.uuid4())

    def _prepare(self) -> dict[str, object]:
        return self.workflow.prepare(
            self.session_id, user_id="deadbeef", environment="dev"
        )

    def test_prepare_reaches_ready_wait_ingest(self) -> None:
        session = self._prepare()
        self.assertEqual(session["status"], "READY_WAIT_INGEST")
        self.assertIsNotNone(session["provider_volume_id"])
        self.assertIsNotNone(session["provider_server_id"])
        self.assertIsNotNone(session["provider_public_ipv4"])
        self.assertEqual(len(self.provider.list_managed_resources()), 2)

    def test_double_prepare_creates_single_vps(self) -> None:
        first = self._prepare()
        second = self._prepare()
        self.assertEqual(
            first["provider_server_id"], second["provider_server_id"]
        )
        self.assertEqual(len(self.provider.list_managed_resources()), 2)

    def test_stop_during_provisioning_reclaims_resources(self) -> None:
        # Simulate a failure partway: provider volume exists, server not yet.
        from provider.conoha import SessionMetadata

        tags = SessionMetadata(
            session_id=self.session_id, user_id="deadbeef", environment="dev"
        ).as_tags()
        self.provider.create_volume("boot", 20, tags)
        session = self.workflow.prepare(
            self.session_id, user_id="deadbeef", environment="dev"
        )
        self.assertEqual(session["status"], "READY_WAIT_INGEST")

        finished = self.workflow.stop(self.session_id)
        self.assertEqual(finished["status"], "FINISHED")
        self.assertEqual(self.provider.list_managed_resources(), [])

    def test_stop_is_idempotent(self) -> None:
        self._prepare()
        first = self.workflow.stop(self.session_id)
        second = self.workflow.stop(self.session_id)
        self.assertEqual(first["status"], "FINISHED")
        self.assertEqual(second["status"], "FINISHED")

    def test_stop_with_delete_failure_keeps_pending_cleanup(self) -> None:
        provider = FailingDeleteProvider(fail_server=True)
        workflow = ProvisioningWorkflow(
            self.store, provider, WorkflowConfig()
        )
        workflow.prepare(
            self.session_id, user_id="deadbeef", environment="dev"
        )

        session = workflow.stop(self.session_id)
        self.assertEqual(session["status"], "FAILED_CLEANUP")
        self.assertTrue(session["cleanup_pending"])
        # Resource stays so the reaper can reclaim it later.
        self.assertEqual(len(provider.list_managed_resources()), 2)

        # Reaper sweep retries and only reaches FAILED after cleanup succeeds.
        provider.fail_server = False
        reaper = Reaper(self.store, provider, ReaperConfig())
        reaper.run()
        session = self.store.get(self.session_id)
        self.assertEqual(session["status"], "FAILED")
        self.assertFalse(session["cleanup_pending"])
        self.assertEqual(provider.list_managed_resources(), [])

    def test_stop_during_blocked_provider_create_cleans_late_resources(self) -> None:
        provider = BlockingCreateProvider()
        workflow = ProvisioningWorkflow(self.store, provider, WorkflowConfig())
        results: list[dict[str, object]] = []
        failures: list[BaseException] = []

        def prepare() -> None:
            try:
                results.append(
                    workflow.prepare(
                        self.session_id,
                        user_id="deadbeef",
                        environment="dev",
                    )
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        thread = threading.Thread(target=prepare)
        thread.start()
        self.assertTrue(provider.create_started.wait(timeout=2))

        stopping = workflow.stop(self.session_id)
        self.assertEqual(stopping["status"], "STOPPING")
        provider.release_create.set()
        thread.join(timeout=5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(results[0]["status"], "FINISHED")
        self.assertEqual(provider.list_managed_resources(), [])

    def test_concurrent_prepare_has_one_provider_owner(self) -> None:
        provider = BlockingCreateProvider()
        workflow = ProvisioningWorkflow(self.store, provider, WorkflowConfig())
        first_result: list[dict[str, object]] = []

        thread = threading.Thread(
            target=lambda: first_result.append(
                workflow.prepare(
                    self.session_id,
                    user_id="deadbeef",
                    environment="dev",
                )
            )
        )
        thread.start()
        self.assertTrue(provider.create_started.wait(timeout=2))

        with self.assertRaises(ProvisioningInProgress):
            workflow.prepare(
                self.session_id,
                user_id="deadbeef",
                environment="dev",
            )

        provider.release_create.set()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(first_result[0]["status"], "READY_WAIT_INGEST")
        self.assertEqual(provider.volume_create_count, 1)
        self.assertEqual(provider.server_create_count, 1)
        self.assertEqual(len(provider.list_managed_resources()), 2)


class SessionStateMachineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = make_store()
        self.provider = FakeProvider()
        self.session_id = str(uuid.uuid4())
        self.session = self.store.create(user_id="deadbeef", environment="dev")
        self.session_id = str(self.session["session_id"])

    def test_invalid_transition_rejected(self) -> None:
        with self.assertRaises(InvalidTransition):
            self.store.transition(self.session_id, "LIVE")

    def test_valid_flow(self) -> None:
        self.store.transition(self.session_id, "PROVISIONING")
        self.store.transition(self.session_id, "BOOTSTRAPPING")
        self.store.transition(self.session_id, "READY_WAIT_INGEST")
        self.store.transition(self.session_id, "LIVE")
        self.store.transition(self.session_id, "HOLDING")
        self.store.transition(self.session_id, "STOPPING")
        self.store.transition(self.session_id, "FINISHED")
        self.assertEqual(self.store.get(self.session_id)["status"], "FINISHED")

    def test_failure_path(self) -> None:
        self.store.transition(self.session_id, "PROVISIONING")
        self.store.transition(
            self.session_id,
            "FAILED_CLEANUP",
            cleanup_pending=True,
            failure_reason="boom",
        )
        self.store.transition(self.session_id, "FAILED", cleanup_pending=False)
        session = self.store.get(self.session_id)
        self.assertEqual(session["status"], "FAILED")
        self.assertFalse(session["cleanup_pending"])


class ReaperTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = make_store()
        self.provider = FakeProvider()
        self.config = ReaperConfig(
            provisioning_timeout_seconds=10,
            no_ingest_timeout_seconds=20,
            hold_timeout_seconds=15,
            orphan_grace_seconds=0,
        )
        self.reaper = Reaper(self.store, self.provider, self.config)

    def test_provisioning_timeout_fails_and_cleans(self) -> None:
        session = self.store.create(user_id="deadbeef", environment="dev")
        session_id = str(session["session_id"])
        self.store.transition(
            session_id,
            "PROVISIONING",
            provisioning_started_at=0,
        )
        from provider.conoha import SessionMetadata

        tags = SessionMetadata(
            session_id=session_id, user_id="deadbeef", environment="dev"
        ).as_tags()
        self.provider.create_volume("boot", 20, tags)

        result = self.reaper.run()
        self.assertEqual(result["timeout_failures"], 1)
        self.assertEqual(self.store.get(session_id)["status"], "FAILED")
        self.assertEqual(self.provider.list_managed_resources(), [])

    def test_reaper_cleans_orphans_for_unknown_session(self) -> None:
        from provider.conoha import SessionMetadata

        # An existing valid state file is the authority needed before a
        # provider-wide orphan sweep may delete anything.
        self.store.create(user_id="cafebabe", environment="dev")
        orphan_id = str(uuid.uuid4())
        tags = SessionMetadata(
            session_id=orphan_id, user_id="deadbeef", environment="dev"
        ).as_tags()
        volume = self.provider.create_volume("orphan-boot", 20, tags)
        server = self.provider.create_server(
            "orphan-node",
            image_ref="ubuntu-24.04",
            flavor_ref="g2",
            volume_id=volume.volume_id,
            metadata=tags,
        )

        result = self.reaper.run()
        self.assertEqual(result["orphan_cleanup"], 2)
        self.assertEqual(self.provider.list_managed_resources(), [])

    def test_reaper_skips_orphans_when_session_state_is_uninitialized(self) -> None:
        from provider.conoha import SessionMetadata

        empty_store = make_store()
        orphan_id = str(uuid.uuid4())
        tags = SessionMetadata(
            session_id=orphan_id, user_id="deadbeef", environment="dev"
        ).as_tags()
        self.provider.create_volume("orphan-boot", 20, tags)

        result = Reaper(empty_store, self.provider, self.config).run()

        self.assertEqual(result["orphan_cleanup"], 0)
        self.assertEqual(len(self.provider.list_managed_resources()), 1)

    def test_reaper_preserves_new_orphan_during_grace_period(self) -> None:
        from provider.conoha import SessionMetadata

        self.store.create(user_id="cafebabe", environment="dev")
        orphan_id = str(uuid.uuid4())
        tags = SessionMetadata(
            session_id=orphan_id,
            user_id="deadbeef",
            environment="dev",
            created_at=900,
        ).as_tags()
        self.provider.create_volume("new-orphan-boot", 20, tags)

        reaper = Reaper(
            self.store,
            self.provider,
            ReaperConfig(orphan_grace_seconds=300),
            now=1_000,
        )
        result = reaper.run()

        self.assertEqual(result["orphan_cleanup"], 0)
        self.assertEqual(len(self.provider.list_managed_resources()), 1)

    def test_reaper_rechecks_session_ownership_before_orphan_delete(self) -> None:
        from provider.conoha import SessionMetadata

        provider = ListHookProvider()
        self.store.create(user_id="cafebabe", environment="dev")
        orphan_id = str(uuid.uuid4())
        tags = SessionMetadata(
            session_id=orphan_id,
            user_id="deadbeef",
            environment="dev",
            created_at=0,
        ).as_tags()
        provider.create_volume("racing-boot", 20, tags)
        def claim_resource() -> None:
            self.store.create(
                session_id=orphan_id,
                user_id="deadbeef",
                environment="dev",
            )
            self.store.transition(orphan_id, "PROVISIONING")

        provider.on_next_list = claim_resource

        result = Reaper(self.store, provider, self.config, now=1_000).run()

        self.assertEqual(result["orphan_cleanup"], 0)
        self.assertEqual(len(provider.list_managed_resources()), 1)

    def test_reaper_aborts_orphan_delete_if_authority_disappears(self) -> None:
        from provider.conoha import SessionMetadata

        provider = ListHookProvider()
        self.store.create(user_id="cafebabe", environment="dev")
        orphan_id = str(uuid.uuid4())
        tags = SessionMetadata(
            session_id=orphan_id,
            user_id="deadbeef",
            environment="dev",
            created_at=0,
        ).as_tags()
        provider.create_volume("orphan-boot", 20, tags)
        provider.on_next_list = self.store.path.unlink

        result = Reaper(self.store, provider, self.config, now=1_000).run()

        self.assertEqual(result["orphan_cleanup"], 0)
        self.assertEqual(len(provider.list_managed_resources()), 1)

    def test_orphan_cleanup_lease_blocks_claim_during_provider_delete(self) -> None:
        from provider.conoha import SessionMetadata

        provider = DeleteHookProvider()
        self.store.create(user_id="authority-anchor", environment="dev")
        orphan_id = str(uuid.uuid4())
        tags = SessionMetadata(
            session_id=orphan_id,
            user_id="deadbeef",
            environment="dev",
            created_at=0,
        ).as_tags()
        volume = provider.create_volume("orphan-boot", 20, tags)
        provider.create_server(
            "orphan-node",
            image_ref="ubuntu-24.04",
            flavor_ref="g2",
            volume_id=volume.volume_id,
            metadata=tags,
        )
        claim_failures: list[BaseException] = []

        def claim_while_delete_is_active() -> None:
            try:
                self.store.create(
                    session_id=orphan_id,
                    user_id="deadbeef",
                    environment="dev",
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                claim_failures.append(exc)

        provider.on_delete_server = claim_while_delete_is_active
        result = Reaper(self.store, provider, self.config, now=1_000).run()

        self.assertEqual(result["orphan_cleanup"], 2)
        self.assertEqual(len(claim_failures), 1)
        self.assertIsInstance(claim_failures[0], OrphanCleanupInProgress)
        self.assertEqual(provider.list_managed_resources(), [])

    def test_absolute_deadline_stops_live_session_and_cleans_resources(self) -> None:
        from provider.conoha import SessionMetadata

        session = self.store.create(user_id="deadbeef", environment="dev")
        session_id = str(session["session_id"])
        self.store.transition(session_id, "PROVISIONING")
        self.store.transition(session_id, "BOOTSTRAPPING")
        self.store.transition(session_id, "READY_WAIT_INGEST")
        self.store.transition(
            session_id,
            "LIVE",
            absolute_deadline_at=999,
        )
        tags = SessionMetadata(
            session_id=session_id,
            user_id="deadbeef",
            environment="dev",
        ).as_tags()
        volume = self.provider.create_volume("boot", 20, tags)
        self.provider.create_server(
            "node",
            image_ref="ubuntu-24.04",
            flavor_ref="g2",
            volume_id=volume.volume_id,
            metadata=tags,
        )

        result = Reaper(self.store, self.provider, self.config, now=1_000).run()

        self.assertEqual(result["deadline_stops"], 1)
        self.assertEqual(self.store.get(session_id)["status"], "FINISHED")
        self.assertEqual(self.provider.list_managed_resources(), [])

    def test_reaper_finishes_failed_cleanup_sessions(self) -> None:
        session = self.store.create(user_id="deadbeef", environment="dev")
        session_id = str(session["session_id"])
        self.store.transition(session_id, "PROVISIONING")
        self.store.transition(
            session_id,
            "FAILED_CLEANUP",
            cleanup_pending=True,
            failure_reason="boom",
        )
        result = self.reaper.run()
        self.assertEqual(result["failed_cleanup_retries"], 1)
        session = self.store.get(session_id)
        self.assertEqual(session["status"], "FAILED")
        self.assertFalse(session["cleanup_pending"])

    def test_reaper_keeps_retrying_after_delete_failure(self) -> None:
        provider = FailingDeleteProvider(fail_server=True)
        reaper = Reaper(self.store, provider, self.config)
        session = self.store.create(user_id="deadbeef", environment="dev")
        session_id = str(session["session_id"])
        self.store.transition(session_id, "PROVISIONING")
        self.store.transition(
            session_id,
            "FAILED_CLEANUP",
            cleanup_pending=True,
            failure_reason="boom",
        )
        from provider.conoha import SessionMetadata

        tags = SessionMetadata(
            session_id=session_id, user_id="deadbeef", environment="dev"
        ).as_tags()
        volume = provider.create_volume("boot", 20, tags)
        provider.create_server(
            "node",
            image_ref="ubuntu-24.04",
            flavor_ref="g2",
            volume_id=volume.volume_id,
            metadata=tags,
        )

        result = reaper.run()
        self.assertEqual(result["failed_cleanup_retries"], 1)
        session = self.store.get(session_id)
        self.assertEqual(session["status"], "FAILED_CLEANUP")
        self.assertTrue(session["cleanup_pending"])
        self.assertEqual(len(provider.list_managed_resources()), 2)

        # Next sweep after the delete starts working completes the cleanup.
        provider.fail_server = False
        result = reaper.run()
        self.assertEqual(result["failed_cleanup_retries"], 1)
        session = self.store.get(session_id)
        self.assertEqual(session["status"], "FAILED")
        self.assertFalse(session["cleanup_pending"])
        self.assertEqual(provider.list_managed_resources(), [])

    def test_reaper_deadline_stop_retries_after_delete_failure(self) -> None:
        provider = FailingDeleteProvider(fail_server=True)
        reaper = Reaper(self.store, provider, self.config)
        session = self.store.create(user_id="deadbeef", environment="dev")
        session_id = str(session["session_id"])
        self.store.transition(session_id, "PROVISIONING")
        self.store.transition(session_id, "BOOTSTRAPPING")
        self.store.transition(session_id, "READY_WAIT_INGEST", ready_at=0)
        from provider.conoha import SessionMetadata

        tags = SessionMetadata(
            session_id=session_id, user_id="deadbeef", environment="dev"
        ).as_tags()
        volume = provider.create_volume("boot", 20, tags)
        provider.create_server(
            "node",
            image_ref="ubuntu-24.04",
            flavor_ref="g2",
            volume_id=volume.volume_id,
            metadata=tags,
        )

        result = reaper.run()
        self.assertEqual(result["deadline_stops"], 1)
        session = self.store.get(session_id)
        self.assertEqual(session["status"], "FAILED_CLEANUP")
        self.assertTrue(session["cleanup_pending"])

        provider.fail_server = False
        result = reaper.run()
        session = self.store.get(session_id)
        self.assertEqual(session["status"], "FAILED")
        self.assertFalse(session["cleanup_pending"])
        self.assertEqual(provider.list_managed_resources(), [])


if __name__ == "__main__":
    unittest.main()
