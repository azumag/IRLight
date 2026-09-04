from __future__ import annotations

import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

import node_internal  # noqa: E402
from node_internal import BootstrapRequest, HeartbeatRequest  # noqa: E402
from pipeline_health import apply_pipeline_health  # noqa: E402
from provider.fake_provider import FakeProvider  # noqa: E402
from reaper import Reaper, ReaperConfig  # noqa: E402
from session_store import SessionStore  # noqa: E402


class PipelineHealthPolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SessionStore(Path(self.tmp.name) / "sessions")
        self.session_id = str(uuid.uuid4())
        self.store.create(
            session_id=self.session_id,
            user_id="user-pipeline",
            environment="dev",
            absolute_deadline_hours=12.0,
        )
        self.store.transition(self.session_id, "PROVISIONING")
        self.store.transition(
            self.session_id,
            "BOOTSTRAPPING",
            provider_server_id="provider-pipeline-1",
            provider_public_ipv4="198.51.100.20",
        )
        self.store.transition(self.session_id, "READY_WAIT_INGEST", ready_at=10.0)
        self.store.bind_node(
            self.session_id,
            node_id="node-0001",
            boot_id="boot-pipeline",
            provider_server_id="provider-pipeline-1",
            registered_at=20.0,
        )
        self.node: dict[str, object] = {
            "node_id": "node-0001",
            "session_id": self.session_id,
            "desired_state": "RUNNING",
        }

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _apply(
        self,
        *,
        at: float,
        media_health: str,
        node_status: str = "STOPPING",
        grace: float = 30.0,
    ) -> bool:
        return apply_pipeline_health(
            self.store,
            node=self.node,
            node_id="node-0001",
            session_id=self.session_id,
            node_status=node_status,
            media_health=media_health,
            observed_at=at,
            grace_seconds=grace,
        )

    def test_transient_pipeline_stop_recovers_without_failing_session(self) -> None:
        self.assertFalse(self._apply(at=100.0, media_health="stopped"))
        self.assertEqual(self.node["media_unhealthy_since"], 100.0)
        self.assertEqual(self.store.get(self.session_id)["status"], "READY_WAIT_INGEST")

        self.assertFalse(
            self._apply(at=120.0, media_health="running", node_status="READY")
        )
        self.assertNotIn("media_unhealthy_since", self.node)
        self.assertNotIn("media_failure_reason", self.node)
        self.assertEqual(self.store.get(self.session_id)["status"], "READY_WAIT_INGEST")

        # A later outage starts a fresh grace window instead of reusing the old one.
        self.assertFalse(self._apply(at=140.0, media_health="stopped"))
        self.assertFalse(self._apply(at=169.9, media_health="stopped"))
        self.assertEqual(self.store.get(self.session_id)["status"], "READY_WAIT_INGEST")

    def test_sustained_pipeline_stop_latches_failed_cleanup(self) -> None:
        self.assertFalse(self._apply(at=100.0, media_health="stopped"))
        self.assertTrue(self._apply(at=130.0, media_health="stopped"))

        session = self.store.get(self.session_id)
        assert session is not None
        self.assertEqual(session["status"], "FAILED_CLEANUP")
        self.assertTrue(session["cleanup_pending"])
        self.assertEqual(session["failure_reason_code"], "PIPELINE_CRASHED")
        self.assertEqual(self.node["desired_state"], "STOPPED")
        self.assertEqual(self.node["pipeline_failure_latched_at"], 130.0)
        self.assertEqual(session["events"][-1]["type"], "session.failure_detected")
        self.assertEqual(session["events"][-1]["reason_code"], "PIPELINE_CRASHED")
        self.assertEqual(session["events"][-1]["payload"]["from_state"], "READY_WAIT_INGEST")
        self.assertEqual(session["events"][-1]["payload"]["to_state"], "FAILED_CLEANUP")

        # Late/replayed fatal samples cannot duplicate the transition/event.
        self.assertFalse(self._apply(at=160.0, media_health="stopped"))
        session = self.store.get(self.session_id)
        assert session is not None
        detected = [
            event
            for event in session["events"]
            if event.get("type") == "session.failure_detected"
        ]
        self.assertEqual(len(detected), 1)

    def test_explicit_failed_status_bypasses_grace(self) -> None:
        self.assertTrue(
            self._apply(
                at=100.0,
                media_health="unknown",
                node_status="FAILED",
                grace=300.0,
            )
        )
        session = self.store.get(self.session_id)
        assert session is not None
        self.assertEqual(session["status"], "FAILED_CLEANUP")
        self.assertEqual(session["failure_reason_code"], "PIPELINE_CRASHED")

    def test_user_stop_wins_over_late_pipeline_failure(self) -> None:
        self.store.transition(
            self.session_id,
            "STOPPING",
            allow_from={"READY_WAIT_INGEST"},
        )
        self.assertFalse(self._apply(at=100.0, media_health="stopped", grace=0.0))
        session = self.store.get(self.session_id)
        assert session is not None
        self.assertEqual(session["status"], "STOPPING")
        self.assertIsNone(session.get("failure_reason_code"))
        self.assertEqual(self.node["desired_state"], "RUNNING")

    def test_reaper_preserves_pipeline_failure_reason_after_cleanup(self) -> None:
        self.assertTrue(self._apply(at=100.0, media_health="stopped", grace=0.0))
        reaper = Reaper(
            self.store,
            FakeProvider(),
            ReaperConfig(),
            now=120.0,
        )
        result = reaper.run()
        self.assertEqual(result["failed_cleanup_retries"], 1)

        session = self.store.get(self.session_id)
        assert session is not None
        self.assertEqual(session["status"], "FAILED")
        self.assertFalse(session["cleanup_pending"])
        self.assertEqual(session["events"][-1]["type"], "session.failed")
        self.assertEqual(session["events"][-1]["reason_code"], "PIPELINE_CRASHED")


class PipelineHealthHeartbeatIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.node_state = self.root / "nodes"
        self.store = SessionStore(self.root / "sessions")
        self.nodes_path = self.node_state / "nodes.json"
        self.tokens_path = self.node_state / "bootstrap_tokens.json"
        self.token = "pipeline-health-bootstrap-token"
        self.old_env = {
            name: os.environ.get(name)
            for name in (
                "NODE_BOOTSTRAP_TOKENS",
                "NODE_BOOTSTRAP_REQUIRE_SESSION_ASSIGNMENT",
                "NODE_MEDIA_HEALTH_FAILURE_GRACE_SECONDS",
            )
        }
        os.environ["NODE_BOOTSTRAP_TOKENS"] = self.token
        os.environ["NODE_BOOTSTRAP_REQUIRE_SESSION_ASSIGNMENT"] = "1"
        os.environ["NODE_MEDIA_HEALTH_FAILURE_GRACE_SECONDS"] = "0"
        self.path_patches = (
            patch.object(node_internal, "NODES_PATH", self.nodes_path),
            patch.object(node_internal, "TOKENS_PATH", self.tokens_path),
            patch.object(node_internal, "STATE_DIR", self.node_state),
            patch.object(node_internal, "default_store", return_value=self.store),
        )
        for item in self.path_patches:
            item.start()
        node_internal.ensure_state()

        self.session_id = str(uuid.uuid4())
        self.store.create(
            session_id=self.session_id,
            user_id="user-heartbeat",
            environment="dev",
            absolute_deadline_hours=12.0,
        )
        self.store.transition(self.session_id, "PROVISIONING")
        self.store.transition(
            self.session_id,
            "BOOTSTRAPPING",
            provider_server_id="provider-heartbeat-1",
            provider_public_ipv4="198.51.100.30",
        )
        self.store.transition(self.session_id, "READY_WAIT_INGEST", ready_at=1.0)

    def tearDown(self) -> None:
        for item in reversed(self.path_patches):
            item.stop()
        for name, value in self.old_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.tmp.cleanup()

    def test_fatal_health_heartbeat_stops_node_and_fails_assigned_session(self) -> None:
        response = node_internal.bootstrap(
            BootstrapRequest(
                provider_server_id="provider-heartbeat-1",
                boot_id="boot-heartbeat",
                agent_version="test",
                bootstrap_request_id="bootstrap-heartbeat-request",
                node_access_token="node-heartbeat-access-token-0123456789abcdef",
                public_address="198.51.100.30",
            ),
            authorization=f"Bearer {self.token}",
        )
        node_id = str(response["node_id"])

        heartbeat = node_internal.heartbeat(
            node_id,
            HeartbeatRequest(
                status="STOPPING",
                media_health="stopped",
                active_publisher=False,
                egress_connected=False,
            ),
            authorization=f"Bearer {response['node_access_token']}",
        )
        self.assertEqual(heartbeat["desired_state"], "STOPPED")

        session = self.store.get(self.session_id)
        assert session is not None
        self.assertEqual(session["status"], "FAILED_CLEANUP")
        self.assertEqual(session["failure_reason_code"], "PIPELINE_CRASHED")
        self.assertEqual(session["events"][-1]["reason_code"], "PIPELINE_CRASHED")

        nodes = node_internal.read_json(self.nodes_path, {})
        node = nodes["nodes"][node_id]
        self.assertEqual(node["desired_state"], "STOPPED")
        self.assertEqual(node["media_failure_reason"], "PIPELINE_CRASHED")
        self.assertNotIn("pipeline_session_event_error", node)


if __name__ == "__main__":
    unittest.main()
