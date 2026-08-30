from __future__ import annotations

import os
import copy
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "continuity"))

# node_internal reads NODE_STATE_DIR at import time; point it at a temp dir.
TEST_STATE = tempfile.mkdtemp(prefix="irlight-node-state-")
os.environ["NODE_STATE_DIR"] = TEST_STATE
os.environ["STATE_DIR"] = TEST_STATE
os.environ["NODE_BOOTSTRAP_TOKENS"] = "spike-token-one,spike-token-two"
os.environ["NODE_ABSOLUTE_DEADLINE_HOURS"] = "12"
os.environ["NODE_EGRESS_URL"] = "rtmp://egress.example/output/relay"
os.environ["NODE_EGRESS_VERIFIED_PEER_IP"] = "198.51.100.10"
os.environ["NODE_INTERNAL_ADMIN_TOKENS"] = "test-admin-token"

sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from node_internal import (  # noqa: E402
    BootstrapRequest,
    HeartbeatRequest,
    IngestObservationRequest,
    NODES_PATH,
    NodeStateError,
    bootstrap,
    ensure_state,
    heartbeat,
    list_nodes,
    stop_node,
)
from secret_files import read_secret_file_or_env  # noqa: E402
from state_safety import initialization_marker  # noqa: E402


class NodeInternalApiTest(unittest.TestCase):
    def setUp(self) -> None:
        # Reset state files so each test starts clean.
        from node_internal import NODES_PATH, TOKENS_PATH

        NODES_PATH.unlink(missing_ok=True)
        TOKENS_PATH.unlink(missing_ok=True)
        initialization_marker(NODES_PATH).unlink(missing_ok=True)
        initialization_marker(TOKENS_PATH).unlink(missing_ok=True)
        ensure_state()

    def _bootstrap(
        self,
        token: str = "spike-token-one",
        *,
        request_id: str = "bootstrap-request-one",
        node_access_token: str = "node-access-token-one-0123456789abcdef",
    ) -> dict[str, object]:
        return bootstrap(
            BootstrapRequest(
                provider_server_id="conoha-abc123",
                boot_id="boot-1",
                agent_version="0.2.0-spike",
                bootstrap_request_id=request_id,
                node_access_token=node_access_token,
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

        nodes = list_nodes(authorization="Bearer test-admin-token")
        self.assertNotIn("tokens", nodes)
        self.assertEqual(len(nodes["nodes"]), 1)
        node = nodes["nodes"][response["node_id"]]
        self.assertEqual(node["provider_server_id"], "conoha-abc123")
        self.assertEqual(node["desired_state"], "RUNNING")
        self.assertNotIn("access_token_sha256", node)
        self.assertTrue(response["node_access_token"])

    def test_identical_bootstrap_retry_returns_same_node(self) -> None:
        first = self._bootstrap()
        second = self._bootstrap()
        self.assertEqual(second["node_id"], first["node_id"])
        self.assertEqual(second["node_access_token"], first["node_access_token"])
        self.assertEqual(
            len(list_nodes(authorization="Bearer test-admin-token")["nodes"]), 1
        )

    def test_bootstrap_token_rejects_a_different_attempt(self) -> None:
        self._bootstrap()
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            self._bootstrap(
                request_id="bootstrap-request-two",
                node_access_token="node-access-token-two-0123456789abcdef",
            )
        self.assertEqual(ctx.exception.status_code, 409)

    def test_concurrent_bootstrap_token_has_one_winner(self) -> None:
        from fastapi import HTTPException

        responses: list[dict[str, object]] = []
        failures: list[BaseException] = []

        def run() -> None:
            try:
                responses.append(self._bootstrap())
            except BaseException as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(len(responses), 2)
        self.assertEqual(failures, [])
        self.assertEqual(responses[0]["node_id"], responses[1]["node_id"])
        self.assertEqual(
            len(list_nodes(authorization="Bearer test-admin-token")["nodes"]), 1
        )

    def test_concurrent_different_attempts_have_one_winner(self) -> None:
        from fastapi import HTTPException

        responses: list[dict[str, object]] = []
        failures: list[BaseException] = []

        def run(index: int) -> None:
            try:
                responses.append(
                    self._bootstrap(
                        request_id=f"bootstrap-request-{index:04d}",
                        node_access_token=f"node-access-token-{index:04d}-0123456789abcdef",
                    )
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        threads = [threading.Thread(target=run, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(len(responses), 1)
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], HTTPException)
        self.assertEqual(failures[0].status_code, 409)

    def test_corrupt_node_state_fails_closed(self) -> None:
        NODES_PATH.write_text("{broken", encoding="utf-8")

        with self.assertRaises(NodeStateError):
            list_nodes(authorization="Bearer test-admin-token")

    def test_nested_bootstrap_authority_corruption_fails_closed(self) -> None:
        import node_internal

        self._bootstrap()
        baseline = node_internal.read_json(NODES_PATH, {})
        digest = next(iter(baseline["tokens"]))
        node_id = next(iter(baseline["nodes"]))
        mutations = {
            "null token record": lambda state: state["tokens"].__setitem__(digest, None),
            "reverted consumed flag": lambda state: state["tokens"][digest].__setitem__(
                "consumed", False
            ),
            "missing referenced node": lambda state: state["nodes"].pop(node_id),
            "access digest mismatch": lambda state: state["tokens"][digest].__setitem__(
                "node_access_token_sha256", "0" * 64
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                state = copy.deepcopy(baseline)
                mutate(state)
                node_internal.atomic_write_json(NODES_PATH, state)
                with self.assertRaises(NodeStateError):
                    self._bootstrap(token="spike-token-two")

    def test_initialized_node_authority_deletion_fails_closed(self) -> None:
        self._bootstrap()
        NODES_PATH.unlink()
        with self.assertRaises(NodeStateError):
            ensure_state()
        with self.assertRaises(NodeStateError):
            self._bootstrap(
                token="spike-token-two",
                request_id="bootstrap-request-after-delete",
                node_access_token="node-access-after-delete-0123456789abcdef",
            )

    def test_deleting_legacy_token_file_does_not_reset_consumption(self) -> None:
        self._bootstrap()
        from node_internal import TOKENS_PATH
        TOKENS_PATH.unlink(missing_ok=True)
        ensure_state()
        with self.assertRaises(NodeStateError):
            self._bootstrap(
                request_id="bootstrap-request-different",
                node_access_token="node-access-token-different-0123456789abcdef",
            )

    def test_legacy_fuse_blocks_reuse_if_canonical_commit_fails(self) -> None:
        from fastapi import HTTPException

        with patch(
            "node_internal._write_authority",
            side_effect=NodeStateError("injected canonical commit failure"),
        ):
            with self.assertRaises(NodeStateError):
                self._bootstrap()

        with self.assertRaises(HTTPException) as failure:
            self._bootstrap(
                request_id="bootstrap-request-after-partial-commit",
                node_access_token="node-access-after-partial-commit-0123456789abcdef",
            )
        self.assertEqual(failure.exception.status_code, 409)

    def test_missing_legacy_fuse_after_partial_commit_fails_closed(self) -> None:
        from node_internal import TOKENS_PATH

        with patch(
            "node_internal._write_authority",
            side_effect=NodeStateError("injected canonical commit failure"),
        ):
            with self.assertRaises(NodeStateError):
                self._bootstrap()

        TOKENS_PATH.unlink()
        with self.assertRaisesRegex(NodeStateError, "fuse disappeared"):
            self._bootstrap(
                request_id="bootstrap-request-after-fuse-loss",
                node_access_token="node-access-after-fuse-loss-0123456789abcdef",
            )

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
            authorization=f"Bearer {response['node_access_token']}",
        )
        self.assertEqual(heartbeat_result["desired_state"], "RUNNING")
        node = list_nodes(authorization="Bearer test-admin-token")["nodes"][node_id]
        self.assertEqual(node["status"], "READY")
        self.assertTrue(node["active_publisher"])
        self.assertIsNotNone(node["last_heartbeat_at"])

    def test_stop_is_idempotent(self) -> None:
        response = self._bootstrap()
        node_id = str(response["node_id"])
        first = stop_node(node_id, authorization="Bearer test-admin-token")
        second = stop_node(node_id, authorization="Bearer test-admin-token")
        self.assertEqual(first["desired_state"], "STOPPED")
        self.assertEqual(second["desired_state"], "STOPPED")
        node = list_nodes(authorization="Bearer test-admin-token")["nodes"][node_id]
        self.assertEqual(node["desired_state"], "STOPPED")
        self.assertEqual(node["status"], "STOPPING")

    def test_node_and_admin_endpoints_reject_missing_tokens(self) -> None:
        from fastapi import HTTPException

        response = self._bootstrap()
        with self.assertRaises(HTTPException) as heartbeat_failure:
            heartbeat(str(response["node_id"]), HeartbeatRequest())
        self.assertEqual(heartbeat_failure.exception.status_code, 401)
        with self.assertRaises(HTTPException) as list_failure:
            list_nodes()
        self.assertEqual(list_failure.exception.status_code, 401)

    def test_heartbeat_rejects_unbounded_or_unknown_nested_fields(self) -> None:
        from pydantic import ValidationError

        common = {
            "status": "ACCEPTED",
            "path": "live/input",
            "online": True,
            "observed_at": 1.0,
        }
        with self.assertRaises(ValidationError):
            IngestObservationRequest(**common, reasons=["x" * 101])
        with self.assertRaises(ValidationError):
            IngestObservationRequest(
                **common,
                tracks=[{"codec": "H264", "attacker_payload": "x"}],
            )


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
