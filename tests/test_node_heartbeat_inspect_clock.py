from __future__ import annotations

import contextlib
import io
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from node_heartbeat_inspect_cli import main as heartbeat_inspect_main  # noqa: E402
from state_safety import initialization_marker  # noqa: E402


def node_authority() -> dict[str, object]:
    return {
        "nodes": {
            "node-0001": {
                "node_id": "node-0001",
                "session_id": "session-0001",
                "provider_server_id": "provider-1",
                "boot_id": "boot-1",
                "agent_version": "test-agent",
                "access_token_sha256": "a" * 64,
                "status": "READY",
                "desired_state": "RUNNING",
                "absolute_deadline": 2000.0,
                "created_at": 900.0,
                "last_heartbeat_at": 990.0,
            }
        },
        "next_node_seq": 2,
        "tokens": {},
    }


class NodeHeartbeatInspectClockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="irlight-heartbeat-clock-")
        self.root = Path(self.tmp.name)
        self.nodes_path = self.root / "nodes.json"
        self.nodes_path.write_text(
            json.dumps(node_authority(), allow_nan=False), encoding="utf-8"
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

    def _run(self, now: float) -> tuple[int, dict[str, object]]:
        output = io.StringIO()
        with patch("node_heartbeat_inspect_cli.time.time", return_value=now):
            with contextlib.redirect_stdout(output):
                result = heartbeat_inspect_main(
                    [
                        "--node-state-dir",
                        str(self.root),
                        "--heartbeat-grace-seconds",
                        "120",
                    ]
                )
        return result, json.loads(output.getvalue())

    def test_invalid_wall_clock_is_redacted_unavailable_without_state_mutation(self) -> None:
        for now in (-1.0, math.nan, math.inf, -math.inf):
            with self.subTest(now=now):
                before = self._snapshot()
                result, payload = self._run(now)

                self.assertEqual(result, 3)
                self.assertEqual(
                    payload,
                    {
                        "reason": "node authority unavailable",
                        "status": "UNAVAILABLE",
                    },
                )
                self.assertEqual(self._snapshot(), before)
                self.assertFalse((self.root / ".node-state.lock").exists())

    def test_unix_epoch_zero_remains_a_valid_clock(self) -> None:
        before = self._snapshot()
        result, payload = self._run(0.0)

        self.assertEqual(result, 0)
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["stale_count"], 0)
        self.assertEqual(self._snapshot(), before)


if __name__ == "__main__":
    unittest.main()
