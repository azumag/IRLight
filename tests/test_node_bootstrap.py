from __future__ import annotations

import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "continuity"))

# node_internal reads NODE_STATE_DIR at import time; point it at a temp dir.
TEST_STATE = tempfile.mkdtemp(prefix="irlight-node-state-")
os.environ["NODE_STATE_DIR"] = TEST_STATE
os.environ["NODE_BOOTSTRAP_TOKENS"] = "spike-token-one,spike-token-two"
os.environ["NODE_ABSOLUTE_DEADLINE_HOURS"] = "12"
os.environ["NODE_EGRESS_URL"] = "rtmp://egress.example/output/relay"

sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from node_internal import (  # noqa: E402
    BootstrapRequest,
    HeartbeatRequest,
    bootstrap,
    ensure_state,
    heartbeat,
    list_nodes,
    stop_node,
)
from secret_files import read_secret_file_or_env  # noqa: E402


class NodeInternalApiTest(unittest.TestCase):
    def setUp(self) -> None:
        ensure_state()
        # Reset state files so each test starts clean.
        from node_internal import NODES_PATH, TOKENS_PATH

        NODES_PATH.unlink(missing_ok=True)
        TOKENS_PATH.unlink(missing_ok=True)
        ensure_state()

    def _bootstrap(self, token: str = "spike-token-one") -> dict[str, object]:
        return bootstrap(
            BootstrapRequest(
                provider_server_id="conoha-abc123",
                boot_id="boot-1",
                agent_version="0.2.0-spike",
                public_address="198.51.100.7",
            ),
            authorization=f"Bearer {token}",
        )

    def test_bootstrap_issues_node_and_delivers_secret_once(self) -> None:
        response = self._bootstrap()
        self.assertEqual(response["status"], "BOOTSTRAPPING")
        self.assertIn("node-", str(response["node_id"]))
        self.assertTrue(str(response["session_id"]))
        self.assertGreater(float(response["absolute_deadline"]), 0)
        self.assertEqual(response["egress_url"], "rtmp://egress.example/output/relay")

        nodes = list_nodes()
        self.assertEqual(len(nodes["nodes"]), 1)
        node = nodes["nodes"][response["node_id"]]
        self.assertEqual(node["provider_server_id"], "conoha-abc123")
        self.assertEqual(node["desired_state"], "RUNNING")

    def test_bootstrap_token_is_one_time(self) -> None:
        self._bootstrap()
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            self._bootstrap()
        self.assertEqual(ctx.exception.status_code, 409)

    def test_unknown_token_rejected(self) -> None:
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            self._bootstrap(token="not-configured")
        self.assertEqual(ctx.exception.status_code, 401)

    def test_heartbeat_updates_node_and_returns_desired_state(self) -> None:
        response = self._bootstrap()
        node_id = str(response["node_id"])
        heartbeat_result = heartbeat(
            node_id,
            HeartbeatRequest(
                status="READY",
                media_health="running",
                active_publisher=True,
                egress_connected=True,
                software_version="0.2.0-spike",
                deadline_remaining_seconds=1000,
            ),
        )
        self.assertEqual(heartbeat_result["desired_state"], "RUNNING")
        node = list_nodes()["nodes"][node_id]
        self.assertEqual(node["status"], "READY")
        self.assertTrue(node["active_publisher"])
        self.assertIsNotNone(node["last_heartbeat_at"])

    def test_stop_is_idempotent(self) -> None:
        response = self._bootstrap()
        node_id = str(response["node_id"])
        first = stop_node(node_id)
        second = stop_node(node_id)
        self.assertEqual(first["desired_state"], "STOPPED")
        self.assertEqual(second["desired_state"], "STOPPED")
        node = list_nodes()["nodes"][node_id]
        self.assertEqual(node["desired_state"], "STOPPED")
        self.assertEqual(node["status"], "STOPPING")


class SecretFileTest(unittest.TestCase):
    def test_egress_url_file_wins_over_env(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "egress_url"
            path.write_text("rtmp://from-file/output/relay\n", encoding="utf-8")
            old_file = os.environ.get("EGRESS_URL_FILE")
            old_env = os.environ.get("EGRESS_URL")
            os.environ["EGRESS_URL_FILE"] = str(path)
            os.environ["EGRESS_URL"] = "rtmp://from-env/output/relay"
            try:
                self.assertEqual(
                    read_secret_file_or_env("EGRESS_URL", "default"),
                    "rtmp://from-file/output/relay",
                )
            finally:
                if old_file is None:
                    os.environ.pop("EGRESS_URL_FILE", None)
                else:
                    os.environ["EGRESS_URL_FILE"] = old_file
                if old_env is None:
                    os.environ.pop("EGRESS_URL", None)
                else:
                    os.environ["EGRESS_URL"] = old_env

    def test_env_fallback_without_file(self) -> None:
        old_file = os.environ.pop("EGRESS_URL_FILE", None)
        old_env = os.environ.get("EGRESS_URL")
        os.environ["EGRESS_URL"] = "rtmp://env-only/output/relay"
        try:
            self.assertEqual(
                read_secret_file_or_env("EGRESS_URL", "default"),
                "rtmp://env-only/output/relay",
            )
        finally:
            if old_file is not None:
                os.environ["EGRESS_URL_FILE"] = old_file
            if old_env is None:
                os.environ.pop("EGRESS_URL", None)
            else:
                os.environ["EGRESS_URL"] = old_env


if __name__ == "__main__":
    unittest.main()
