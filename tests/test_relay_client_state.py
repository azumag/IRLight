from __future__ import annotations

import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "node-agent"))
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

import node_internal  # noqa: E402
from node_internal import (  # noqa: E402
    BootstrapRequest,
    HeartbeatRequest,
    RelayClientObservationRequest,
)
from relay_client import RelayClientConfig, RelayClientObserver  # noqa: E402
from session_store import SessionStore  # noqa: E402


class RelayClientObserverTest(unittest.TestCase):
    def _observer(self) -> RelayClientObserver:
        return RelayClientObserver(
            RelayClientConfig(
                api_url="http://mediamtx.invalid:9997",
                path="output/relay",
                timeout_seconds=0.2,
            )
        )

    def test_reader_count_maps_to_safe_connection_state(self) -> None:
        observer = self._observer()
        cases = [
            ([], "DISCONNECTED", False, 0),
            ([{"id": "client-1"}], "CONNECTED", True, 1),
        ]
        for readers, expected_status, expected_connected, count in cases:
            with self.subTest(status=expected_status):
                with patch.object(
                    observer,
                    "_path_snapshot",
                    return_value={"online": True, "readers": readers},
                ):
                    result = observer.observe(now=123.0)
                self.assertEqual(result["status"], expected_status)
                self.assertEqual(result["connected"], expected_connected)
                self.assertEqual(result["reader_count"], count)
                self.assertIsNone(result["reason_code"])
                self.assertEqual(result["observed_at"], 123.0)

    def test_missing_or_offline_path_is_fail_safe(self) -> None:
        observer = self._observer()
        for snapshot in [None, {"online": False, "readers": [{"id": "x"}]}]:
            with self.subTest(snapshot=snapshot), patch.object(
                observer,
                "_path_snapshot",
                return_value=snapshot,
            ):
                result = observer.observe(now=124.0)
            self.assertEqual(result["status"], "UNKNOWN")
            self.assertFalse(result["connected"])
            self.assertEqual(result["reader_count"], 0)
            self.assertEqual(result["reason_code"], "RELAY_SOURCE_OFFLINE")


class RelayClientHeartbeatTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = SessionStore(root / "sessions")
        self.nodes_path = root / "nodes" / "nodes.json"
        self.tokens_path = root / "nodes" / "tokens.json"
        self.old_token = os.environ.get("NODE_BOOTSTRAP_TOKENS")
        self.old_require = os.environ.get("NODE_BOOTSTRAP_REQUIRE_SESSION_ASSIGNMENT")
        os.environ["NODE_BOOTSTRAP_TOKENS"] = "relay-state-token"
        os.environ["NODE_BOOTSTRAP_REQUIRE_SESSION_ASSIGNMENT"] = "1"
        self.patches = (
            patch.object(node_internal, "NODES_PATH", self.nodes_path),
            patch.object(node_internal, "TOKENS_PATH", self.tokens_path),
            patch.object(node_internal, "STATE_DIR", root / "nodes"),
            patch.object(node_internal, "default_store", return_value=self.store),
        )
        for item in self.patches:
            item.start()
        node_internal.ensure_state()

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        if self.old_token is None:
            os.environ.pop("NODE_BOOTSTRAP_TOKENS", None)
        else:
            os.environ["NODE_BOOTSTRAP_TOKENS"] = self.old_token
        if self.old_require is None:
            os.environ.pop("NODE_BOOTSTRAP_REQUIRE_SESSION_ASSIGNMENT", None)
        else:
            os.environ["NODE_BOOTSTRAP_REQUIRE_SESSION_ASSIGNMENT"] = self.old_require
        self.tmp.cleanup()

    def test_heartbeat_audits_and_persists_relay_client_transitions(self) -> None:
        session_id = str(uuid.uuid4())
        self.store.create(
            session_id=session_id,
            user_id="user-1",
            environment="dev",
            egress_mode="RELAY_ONLY",
        )
        self.store.transition(session_id, "PROVISIONING")
        self.store.transition(
            session_id,
            "BOOTSTRAPPING",
            provider_server_id="relay-provider",
        )
        self.store.transition(session_id, "READY_WAIT_INGEST", ready_at=100.0)
        bootstrap = node_internal.bootstrap(
            BootstrapRequest(
                provider_server_id="relay-provider",
                boot_id="boot-relay",
                agent_version="test",
            ),
            authorization="Bearer relay-state-token",
        )

        def heartbeat(connected: bool, reader_count: int):
            return node_internal.heartbeat(
                str(bootstrap["node_id"]),
                HeartbeatRequest(
                    status="READY",
                    media_health="running",
                    relay_client=RelayClientObservationRequest(
                        status="CONNECTED" if connected else "DISCONNECTED",
                        connected=connected,
                        reader_count=reader_count,
                        observed_at=200.0,
                    ),
                ),
            )

        heartbeat(True, 1)
        heartbeat(False, 0)
        heartbeat(True, 2)
        session = self.store.get(session_id)
        assert session is not None
        self.assertEqual(
            [event["type"] for event in session["events"]],
            [
                "relay.client.connected",
                "relay.client.disconnected",
                "relay.client.reconnected",
            ],
        )
        self.assertTrue(session["relay_client_connected"])
        self.assertEqual(session["relay_client_status"], "CONNECTED")
        self.assertEqual(session["relay_client_reader_count"], 2)
        self.assertEqual(session["events"][-1]["payload"]["reader_count"], 2)


if __name__ == "__main__":
    unittest.main()
