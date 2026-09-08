from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from node_heartbeat_inspect_cli import (  # noqa: E402
    main as heartbeat_inspect_main,
    summarize_node_heartbeats,
)
from state_safety import initialization_marker  # noqa: E402


def node_authority(*, last_heartbeat_at: float | None = 990.0) -> dict[str, object]:
    return {
        "nodes": {
            "node-0001": {
                "node_id": "node-0001",
                "session_id": "session-0001",
                "provider_server_id": "provider-secretish-id",
                "boot_id": "boot-secretish-id",
                "agent_version": "test-agent",
                "access_token_sha256": "a" * 64,
                "status": "READY",
                "desired_state": "RUNNING",
                "absolute_deadline": 2000.0,
                "created_at": 900.0,
                "last_heartbeat_at": last_heartbeat_at,
            }
        },
        "next_node_seq": 2,
        "tokens": {},
    }


class NodeHeartbeatSummaryTest(unittest.TestCase):
    def test_expected_running_node_becomes_stale_at_grace_boundary(self) -> None:
        payload = node_authority(last_heartbeat_at=880.0)
        summary = summarize_node_heartbeats(payload, now=1000.0, grace_seconds=120.0)

        self.assertEqual(summary["status"], "STALE")
        self.assertEqual(summary["stale_count"], 1)
        node = summary["nodes"][0]
        self.assertTrue(node["expected_heartbeat"])
        self.assertTrue(node["stale"])
        self.assertEqual(node["heartbeat_age_seconds"], 120.0)
        self.assertIsNone(node["registration_age_seconds"])

    def test_terminal_or_intentionally_stopped_node_does_not_raise_heartbeat_alert(self) -> None:
        payload = node_authority(last_heartbeat_at=1.0)
        node = payload["nodes"]["node-0001"]
        node["status"] = "STOPPED"
        node["desired_state"] = "STOPPED"

        summary = summarize_node_heartbeats(payload, now=1000.0, grace_seconds=120.0)

        self.assertEqual(summary["status"], "OK")
        self.assertEqual(summary["expected_running_count"], 0)
        self.assertFalse(summary["nodes"][0]["stale"])

    def test_never_heartbeated_node_uses_registration_age_and_future_clock_is_clamped(self) -> None:
        payload = node_authority(last_heartbeat_at=None)
        payload["nodes"]["node-0001"]["created_at"] = 1010.0

        summary = summarize_node_heartbeats(payload, now=1000.0, grace_seconds=120.0)
        node = summary["nodes"][0]

        self.assertFalse(node["heartbeat_seen"])
        self.assertIsNone(node["heartbeat_age_seconds"])
        self.assertEqual(node["registration_age_seconds"], 0.0)
        self.assertFalse(node["stale"])


class NodeHeartbeatInspectCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="irlight-heartbeat-inspect-")
        self.root = Path(self.tmp.name)
        self.nodes_path = self.root / "nodes.json"
        self.nodes_path.write_text(
            json.dumps(node_authority(last_heartbeat_at=800.0), allow_nan=False),
            encoding="utf-8",
        )
        initialization_marker(self.nodes_path).write_text("v1\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _snapshot(self) -> dict[str, tuple[bytes, int]]:
        result: dict[str, tuple[bytes, int]] = {}
        for path in sorted(self.root.rglob("*")):
            if path.is_file() and not path.is_symlink():
                item = path.stat()
                result[str(path.relative_to(self.root))] = (
                    path.read_bytes(),
                    item.st_mtime_ns,
                )
        return result

    def test_cli_is_read_only_machine_readable_and_redacts_node_secrets(self) -> None:
        before = self._snapshot()
        output = io.StringIO()
        with patch("node_heartbeat_inspect_cli.time.time", return_value=1000.0):
            with contextlib.redirect_stdout(output):
                result = heartbeat_inspect_main(
                    ["--node-state-dir", str(self.root), "--heartbeat-grace-seconds", "120"]
                )

        self.assertEqual(result, 2)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "STALE")
        self.assertEqual(payload["nodes"][0]["node_id"], "node-0001")
        self.assertEqual(payload["nodes"][0]["session_id"], "session-0001")
        rendered = output.getvalue()
        self.assertNotIn("provider-secretish-id", rendered)
        self.assertNotIn("boot-secretish-id", rendered)
        self.assertNotIn("a" * 64, rendered)
        self.assertEqual(self._snapshot(), before)
        self.assertFalse((self.root / ".node-state.lock").exists())

    def test_missing_initialization_marker_is_unavailable_without_creating_state(self) -> None:
        initialization_marker(self.nodes_path).unlink()
        before = self._snapshot()
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            result = heartbeat_inspect_main(["--node-state-dir", str(self.root)])

        self.assertEqual(result, 3)
        self.assertEqual(
            json.loads(output.getvalue()),
            {"reason": "node authority unavailable", "status": "UNAVAILABLE"},
        )
        self.assertEqual(self._snapshot(), before)
        self.assertFalse(initialization_marker(self.nodes_path).exists())

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink is unavailable")
    def test_symlinked_node_authority_is_rejected(self) -> None:
        target = self.root / "replacement.json"
        target.write_text(self.nodes_path.read_text(encoding="utf-8"), encoding="utf-8")
        self.nodes_path.unlink()
        self.nodes_path.symlink_to(target)
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            result = heartbeat_inspect_main(["--node-state-dir", str(self.root)])

        self.assertEqual(result, 3)
        self.assertEqual(json.loads(output.getvalue())["status"], "UNAVAILABLE")

    def test_invalid_env_grace_is_reported_as_argument_error(self) -> None:
        stderr = io.StringIO()
        with patch.dict(os.environ, {"NODE_HEARTBEAT_GRACE_SECONDS": "nan"}):
            with contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as raised:
                    heartbeat_inspect_main(["--node-state-dir", str(self.root)])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("finite positive number", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
