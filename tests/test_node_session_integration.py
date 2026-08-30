from __future__ import annotations

import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

import node_internal  # noqa: E402
from node_internal import BootstrapRequest, HeartbeatRequest, IngestObservationRequest  # noqa: E402
from session_store import SessionStore  # noqa: E402


class NodeSessionIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.node_state = self.root / "nodes"
        self.session_state = self.root / "sessions"
        # This baseline integration test predates the recovery timing gate and
        # focuses on event/state mapping. Dedicated tests cover stable recovery.
        self.store = SessionStore(self.session_state, recovery_stable_seconds=0.0)
        self.nodes_path = self.node_state / "nodes.json"
        self.tokens_path = self.node_state / "bootstrap_tokens.json"
        self.token = "session-integration-token"
        self.node_tokens: dict[str, str] = {}
        self.old_tokens = os.environ.get("NODE_BOOTSTRAP_TOKENS")
        self.old_require = os.environ.get("NODE_BOOTSTRAP_REQUIRE_SESSION_ASSIGNMENT")
        os.environ["NODE_BOOTSTRAP_TOKENS"] = self.token
        os.environ["NODE_BOOTSTRAP_REQUIRE_SESSION_ASSIGNMENT"] = "1"
        self.path_patches = (
            patch.object(node_internal, "NODES_PATH", self.nodes_path),
            patch.object(node_internal, "TOKENS_PATH", self.tokens_path),
            patch.object(node_internal, "STATE_DIR", self.node_state),
            patch.object(node_internal, "default_store", return_value=self.store),
        )
        for item in self.path_patches:
            item.start()
        node_internal.ensure_state()

    def tearDown(self) -> None:
        for item in reversed(self.path_patches):
            item.stop()
        if self.old_tokens is None:
            os.environ.pop("NODE_BOOTSTRAP_TOKENS", None)
        else:
            os.environ["NODE_BOOTSTRAP_TOKENS"] = self.old_tokens
        if self.old_require is None:
            os.environ.pop("NODE_BOOTSTRAP_REQUIRE_SESSION_ASSIGNMENT", None)
        else:
            os.environ["NODE_BOOTSTRAP_REQUIRE_SESSION_ASSIGNMENT"] = self.old_require
        self.tmp.cleanup()

    def _prepared_session(self, provider_server_id: str = "provider-server-1") -> str:
        session_id = str(uuid.uuid4())
        self.store.create(
            session_id=session_id,
            user_id="user-1",
            environment="dev",
            absolute_deadline_hours=12.0,
        )
        self.store.transition(session_id, "PROVISIONING")
        self.store.transition(
            session_id,
            "BOOTSTRAPPING",
            provider_server_id=provider_server_id,
            provider_public_ipv4="198.51.100.10",
        )
        self.store.transition(
            session_id,
            "READY_WAIT_INGEST",
            ready_at=100.0,
        )
        return session_id

    def _bootstrap(self, provider_server_id: str) -> dict[str, object]:
        response = node_internal.bootstrap(
            BootstrapRequest(
                provider_server_id=provider_server_id,
                boot_id="boot-session-1",
                agent_version="test",
                bootstrap_request_id="bootstrap-session-request-1",
                node_access_token="node-session-access-token-0123456789abcdef",
                public_address="198.51.100.10",
            ),
            authorization=f"Bearer {self.token}",
        )
        self.node_tokens[str(response["node_id"])] = str(response["node_access_token"])
        return response

    def _heartbeat(self, node_id: str, request: HeartbeatRequest):
        return node_internal.heartbeat(
            node_id,
            request,
            authorization=f"Bearer {self.node_tokens[node_id]}",
        )

    @staticmethod
    def _ingest(
        *,
        status: str,
        online: bool,
        source_id: str | None,
        reasons: list[str] | None = None,
    ) -> IngestObservationRequest:
        return IngestObservationRequest(
            status=status,
            path="live/input",
            online=online,
            source_type="rtmpConn" if online else None,
            source_id=source_id,
            bitrate_bps=1_500_000 if online else None,
            max_bitrate_bps=6_000_000,
            tracks=(
                [
                    {"codec": "H264", "width": 1280, "height": 720},
                    {"codec": "MPEG-4 Audio", "sampleRate": 48000, "channelCount": 2},
                ]
                if online
                else []
            ),
            reasons=reasons or [],
            warnings=[],
            quality={"video_fps": 30.0} if online else None,
            enforced=False,
            observed_at=123.0,
        )

    def test_bootstrap_binds_provider_node_to_existing_user_session(self) -> None:
        session_id = self._prepared_session()
        response = self._bootstrap("provider-server-1")
        self.assertEqual(response["session_id"], session_id)
        self.assertTrue(response["session_assigned"])

        session = self.store.get(session_id)
        self.assertIsNotNone(session)
        assert session is not None
        self.assertEqual(session["node_id"], response["node_id"])
        self.assertEqual(session["node_boot_id"], "boot-session-1")
        self.assertIsNotNone(session["node_registered_at"])

    def test_internal_auth_token_is_bound_to_assigned_session(self) -> None:
        session_id = self._prepared_session()
        response = self._bootstrap("provider-server-1")
        authorization = f"Bearer {response['node_access_token']}"

        matched = node_internal.require_assigned_node(
            authorization,
            session_id=session_id,
        )
        self.assertEqual(matched["node_id"], response["node_id"])

        for supplied, requested_session in (
            (None, session_id),
            ("Bearer wrong-token", session_id),
            (authorization, str(uuid.uuid4())),
        ):
            with self.subTest(supplied=supplied, requested_session=requested_session):
                with self.assertRaises(HTTPException) as failure:
                    node_internal.require_assigned_node(
                        supplied,
                        session_id=requested_session,
                    )
                self.assertEqual(failure.exception.status_code, 401)

    def test_unassigned_provider_is_rejected_in_strict_mode_without_consuming_token(self) -> None:
        with self.assertRaises(HTTPException) as failure:
            self._bootstrap("unknown-provider")
        self.assertEqual(failure.exception.status_code, 409)

        # The failed assignment must not burn the one-time bootstrap token.
        self._prepared_session("unknown-provider")
        response = self._bootstrap("unknown-provider")
        self.assertTrue(response["session_assigned"])

    def test_ingest_heartbeat_updates_formal_session_lifecycle(self) -> None:
        session_id = self._prepared_session()
        response = self._bootstrap("provider-server-1")
        node_id = str(response["node_id"])

        self._heartbeat(
            node_id,
            HeartbeatRequest(
                status="READY",
                media_health="running",
                active_publisher=True,
                egress_connected=True,
                ingest=self._ingest(status="ACCEPTED", online=True, source_id="source-1"),
            ),
        )
        session = self.store.get(session_id)
        self.assertIsNotNone(session)
        assert session is not None
        self.assertEqual(session["status"], "LIVE")
        self.assertIsNotNone(session["first_ingest_at"])
        self.assertEqual(
            [event["type"] for event in session["events"]],
            ["ingest.connected", "ingest.format_detected", "session.live"],
        )
        self.assertEqual(session["events"][-1]["payload"]["from_state"], "READY_WAIT_INGEST")
        self.assertEqual(session["events"][-1]["payload"]["to_state"], "LIVE")
        for event in session["events"]:
            self.assertEqual(event["origin"], "node-agent")
            self.assertEqual(event["payload"]["node_id"], node_id)
            self.assertNotIn("credential_secret", event["payload"])

        self._heartbeat(
            node_id,
            HeartbeatRequest(
                status="READY",
                media_health="running",
                active_publisher=True,
                egress_connected=True,
                ingest=self._ingest(
                    status="DEGRADED",
                    online=True,
                    source_id="source-1",
                    reasons=["FPS_OUT_OF_RANGE"],
                ),
            ),
        )
        session = self.store.get(session_id)
        assert session is not None
        self.assertEqual(session["status"], "DEGRADED")
        self.assertEqual(
            [event["type"] for event in session["events"][-2:]],
            ["ingest.degraded", "session.degraded"],
        )
        self.assertEqual(session["events"][-1]["reason_code"], "FPS_OUT_OF_RANGE")
        self.assertEqual(session["events"][-1]["payload"]["from_state"], "LIVE")
        self.assertEqual(session["events"][-1]["payload"]["to_state"], "DEGRADED")

        self._heartbeat(
            node_id,
            HeartbeatRequest(
                status="READY",
                media_health="running",
                active_publisher=True,
                egress_connected=True,
                ingest=self._ingest(status="ACCEPTED", online=True, source_id="source-1"),
            ),
        )
        session = self.store.get(session_id)
        assert session is not None
        self.assertEqual(session["status"], "LIVE")
        self.assertEqual(
            [event["type"] for event in session["events"][-2:]],
            ["ingest.recovered", "session.recovered"],
        )
        self.assertEqual(session["events"][-1]["payload"]["from_state"], "DEGRADED")
        self.assertEqual(session["events"][-1]["payload"]["to_state"], "LIVE")

        self._heartbeat(
            node_id,
            HeartbeatRequest(
                status="READY",
                media_health="running",
                active_publisher=False,
                egress_connected=True,
                ingest=self._ingest(status="OFFLINE", online=False, source_id=None),
            ),
        )
        session = self.store.get(session_id)
        assert session is not None
        self.assertEqual(session["status"], "HOLDING")
        self.assertEqual(
            [event["type"] for event in session["events"][-2:]],
            ["ingest.disconnected", "session.holding"],
        )
        self.assertEqual(session["events"][-1]["reason_code"], "INGEST_DISCONNECTED")
        self.assertIsNotNone(session["last_ingest_at"])

        self._heartbeat(
            node_id,
            HeartbeatRequest(
                status="READY",
                media_health="running",
                active_publisher=True,
                egress_connected=True,
                ingest=self._ingest(
                    status="DEGRADED",
                    online=True,
                    source_id="source-2",
                    reasons=["FPS_OUT_OF_RANGE"],
                ),
            ),
        )
        session = self.store.get(session_id)
        assert session is not None
        self.assertEqual(session["status"], "DEGRADED")
        self.assertEqual(
            [event["type"] for event in session["events"][-4:]],
            [
                "ingest.reconnected",
                "ingest.format_detected",
                "ingest.degraded",
                "session.recovered",
            ],
        )
        self.assertEqual(session["events"][-1]["reason_code"], "FPS_OUT_OF_RANGE")
        self.assertEqual(session["events"][-1]["payload"]["from_state"], "HOLDING")
        self.assertEqual(session["events"][-1]["payload"]["to_state"], "DEGRADED")
        self.assertIsNone(session["hold_deadline_at"])

        self._heartbeat(
            node_id,
            HeartbeatRequest(
                status="READY",
                media_health="running",
                active_publisher=False,
                egress_connected=True,
                ingest=self._ingest(status="OFFLINE", online=False, source_id=None),
            ),
        )
        session = self.store.get(session_id)
        assert session is not None
        self.assertEqual(session["status"], "HOLDING")
        self.assertEqual(session["events"][-1]["type"], "session.holding")
        self.assertEqual(session["events"][-1]["payload"]["from_state"], "DEGRADED")

    def test_first_usable_ingest_can_start_degraded(self) -> None:
        session_id = self._prepared_session()
        response = self._bootstrap("provider-server-1")
        node_id = str(response["node_id"])

        self._heartbeat(
            node_id,
            HeartbeatRequest(
                status="READY",
                media_health="running",
                active_publisher=True,
                egress_connected=True,
                ingest=self._ingest(
                    status="DEGRADED",
                    online=True,
                    source_id="source-1",
                    reasons=["BITRATE_TOO_LOW"],
                ),
            ),
        )
        session = self.store.get(session_id)
        assert session is not None
        self.assertEqual(session["status"], "DEGRADED")
        self.assertIsNotNone(session["first_ingest_at"])
        self.assertEqual(session["events"][-1]["type"], "session.degraded")
        self.assertEqual(session["events"][-1]["reason_code"], "BITRATE_TOO_LOW")
        self.assertEqual(session["events"][-1]["payload"]["from_state"], "READY_WAIT_INGEST")
        self.assertEqual(session["events"][-1]["payload"]["to_state"], "DEGRADED")


if __name__ == "__main__":
    unittest.main()
