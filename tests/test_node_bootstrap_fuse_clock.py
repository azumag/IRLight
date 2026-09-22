from __future__ import annotations

import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "continuity"))
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

import node_internal  # noqa: E402
from state_safety import initialization_marker  # noqa: E402


class BootstrapTokenFuseClockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="irlight-fuse-clock-")
        self.state_dir = Path(self.temporary.name)
        self.tokens_path = self.state_dir / "bootstrap_tokens.json"
        self.nodes_path = self.state_dir / "nodes.json"
        self.patchers = [
            patch.object(node_internal, "STATE_DIR", self.state_dir),
            patch.object(node_internal, "TOKENS_PATH", self.tokens_path),
            patch.object(node_internal, "NODES_PATH", self.nodes_path),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temporary.cleanup()

    def _write(self, token: str = "clock-test-token") -> str:
        digest = node_internal.hash_token(token)
        node_internal._write_legacy_token_fuse(
            digest,
            node_id="node-clock-test",
            session_id="session-clock-test",
        )
        return digest

    def test_invalid_default_clock_does_not_create_fuse_or_marker(self) -> None:
        invalid_values = (-1.0, math.nan, math.inf, -math.inf)

        for value in invalid_values:
            with self.subTest(value=value):
                self.tokens_path.unlink(missing_ok=True)
                initialization_marker(self.tokens_path).unlink(missing_ok=True)
                with patch.object(node_internal.time, "time", return_value=value):
                    with self.assertRaises(node_internal.NodeStateError):
                        self._write()
                self.assertFalse(self.tokens_path.exists())
                self.assertFalse(initialization_marker(self.tokens_path).exists())

    def test_invalid_default_clock_preserves_existing_fuse_bytes_and_mtime(self) -> None:
        with patch.object(node_internal.time, "time", return_value=10.0):
            self._write("first-token")

        before = self.tokens_path.read_bytes()
        before_mtime_ns = self.tokens_path.stat().st_mtime_ns
        before_marker = initialization_marker(self.tokens_path).read_bytes()

        with patch.object(node_internal.time, "time", return_value=-1.0):
            with self.assertRaises(node_internal.NodeStateError):
                self._write("second-token")

        self.assertEqual(self.tokens_path.read_bytes(), before)
        self.assertEqual(self.tokens_path.stat().st_mtime_ns, before_mtime_ns)
        self.assertEqual(initialization_marker(self.tokens_path).read_bytes(), before_marker)

    def test_epoch_zero_is_published_and_accepted_by_reader_validator(self) -> None:
        with patch.object(node_internal.time, "time", return_value=0.0):
            digest = self._write()

        persisted = node_internal.read_json(self.tokens_path, {})
        validated = node_internal._validate_tokens(persisted)
        self.assertEqual(validated["tokens"][digest]["consumed_at"], 0.0)
        self.assertTrue(initialization_marker(self.tokens_path).exists())

    def test_writer_validates_completed_payload_before_publish(self) -> None:
        original_validate = node_internal._validate_tokens

        def reject_completed_payload(payload: dict[str, object]) -> dict[str, object]:
            if payload.get("tokens"):
                raise node_internal.NodeStateError("synthetic invalid token payload")
            return original_validate(payload)

        with (
            patch.object(node_internal.time, "time", return_value=10.0),
            patch.object(node_internal, "_validate_tokens", side_effect=reject_completed_payload),
        ):
            with self.assertRaisesRegex(
                node_internal.NodeStateError, "synthetic invalid token payload"
            ):
                self._write()

        self.assertFalse(self.tokens_path.exists())
        self.assertFalse(initialization_marker(self.tokens_path).exists())


if __name__ == "__main__":
    unittest.main()
