from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from auth_store import _default_sessions, _default_users  # noqa: E402
from control_store import default_control  # noqa: E402
from state_readiness import StateReadinessError, check_state_readiness  # noqa: E402
from state_safety import initialization_marker  # noqa: E402


class StateReadinessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="irlight-readyz-")
        self.root = Path(self.tmp.name)
        self.state_dir = self.root / "control"
        self.node_state_dir = self.root / "node"
        self.state_dir.mkdir()
        self.node_state_dir.mkdir()

        self._write_authority(
            self.state_dir / "control.json", default_control(now=1.0)
        )
        self._write_authority(
            self.state_dir / "catalog.json", {"destinations": {}, "assets": {}}
        )
        self._write_authority(self.state_dir / "users.json", _default_users())
        self._write_authority(
            self.state_dir / "auth_sessions.json", _default_sessions()
        )
        self._write_authority(
            self.node_state_dir / "nodes.json",
            {"nodes": {}, "next_node_seq": 1, "tokens": {}},
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def _write_authority(
        path: Path, payload: dict[str, object], *, marker: bool = True
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, allow_nan=False, sort_keys=True), encoding="utf-8"
        )
        if marker:
            initialization_marker(path).write_text("v1\n", encoding="utf-8")

    def _snapshot(self) -> dict[str, tuple[bytes, int]]:
        result: dict[str, tuple[bytes, int]] = {}
        for path in sorted(self.root.rglob("*")):
            if path.is_file() and not path.is_symlink():
                stat_result = path.stat()
                result[str(path.relative_to(self.root))] = (
                    path.read_bytes(),
                    stat_result.st_mtime_ns,
                )
        return result

    def _check(self) -> None:
        check_state_readiness(
            state_dir=self.state_dir,
            node_state_dir=self.node_state_dir,
        )

    def test_valid_readiness_does_not_create_or_rewrite_state(self) -> None:
        before = self._snapshot()
        self._check()
        after = self._snapshot()

        self.assertEqual(after, before)
        self.assertFalse((self.state_dir / ".control-state.lock").exists())
        self.assertFalse((self.state_dir / ".auth-state.lock").exists())
        self.assertFalse((self.state_dir / ".catalog.lock").exists())
        self.assertFalse((self.node_state_dir / ".node-state.lock").exists())

    def test_missing_authority_after_marker_is_not_recreated(self) -> None:
        users_path = self.state_dir / "users.json"
        users_path.unlink()

        with self.assertRaises(StateReadinessError):
            self._check()

        self.assertFalse(users_path.exists())
        self.assertTrue(initialization_marker(users_path).exists())

    def test_missing_initialization_marker_is_not_ready(self) -> None:
        catalog_path = self.state_dir / "catalog.json"
        initialization_marker(catalog_path).unlink()

        with self.assertRaises(StateReadinessError):
            self._check()

        self.assertFalse(initialization_marker(catalog_path).exists())

    def test_corrupt_authority_fails_without_rewrite(self) -> None:
        sessions_path = self.state_dir / "auth_sessions.json"
        sessions_path.write_text('{"sessions":', encoding="utf-8")
        before = sessions_path.read_bytes()

        with self.assertRaises(StateReadinessError):
            self._check()

        self.assertEqual(sessions_path.read_bytes(), before)

    def test_catalog_with_credential_bearing_url_is_not_ready(self) -> None:
        catalog_path = self.state_dir / "catalog.json"
        catalog = {
            "destinations": {
                "unsafe": {
                    "server_url": "rtmp://user:secret@example.invalid/live",
                }
            },
            "assets": {},
        }
        catalog_path.write_text(json.dumps(catalog), encoding="utf-8")

        with self.assertRaises(StateReadinessError):
            self._check()

    def test_legacy_token_fuse_marker_without_file_is_not_ready(self) -> None:
        legacy = self.node_state_dir / "bootstrap_tokens.json"
        initialization_marker(legacy).write_text("v1\n", encoding="utf-8")

        with self.assertRaises(StateReadinessError):
            self._check()

        self.assertFalse(legacy.exists())

    def test_valid_legacy_token_fuse_without_marker_remains_compatible(self) -> None:
        legacy = self.node_state_dir / "bootstrap_tokens.json"
        digest = "a" * 64
        legacy.write_text(
            json.dumps(
                {
                    "tokens": {
                        digest: {
                            "consumed": True,
                            "consumed_at": 1.0,
                            "node_id": "node-1",
                            "session_id": "session-1",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        before = self._snapshot()
        self._check()
        self.assertEqual(self._snapshot(), before)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink is unavailable")
    def test_symlinked_authority_is_rejected(self) -> None:
        target = self.root / "replacement.json"
        target.write_text(json.dumps(_default_users()), encoding="utf-8")
        users_path = self.state_dir / "users.json"
        users_path.unlink()
        users_path.symlink_to(target)

        with self.assertRaises(StateReadinessError):
            self._check()


if __name__ == "__main__":
    unittest.main()
