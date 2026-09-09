from __future__ import annotations

import contextlib
import io
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
from state_inspect_cli import main as inspect_main  # noqa: E402
from state_readiness import (  # noqa: E402
    StateReadinessError,
    check_state_readiness,
    inspect_state_readiness,
)
from state_safety import initialization_marker  # noqa: E402


@unittest.skipUnless(hasattr(os, "symlink"), "symlink is unavailable")
class StateReadinessRootSafetyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="irlight-readyz-root-")
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
    def _write_authority(path: Path, payload: dict[str, object]) -> None:
        path.write_text(
            json.dumps(payload, allow_nan=False, sort_keys=True), encoding="utf-8"
        )
        initialization_marker(path).write_text("v1\n", encoding="utf-8")

    def test_symlinked_control_root_is_rejected_without_following(self) -> None:
        real_state_dir = self.root / "control-real"
        self.state_dir.rename(real_state_dir)
        self.state_dir.symlink_to(real_state_dir, target_is_directory=True)

        with self.assertRaises(StateReadinessError):
            check_state_readiness(
                state_dir=self.state_dir,
                node_state_dir=self.node_state_dir,
            )

        checks = {
            check["authority"]: check
            for check in inspect_state_readiness(
                state_dir=self.state_dir,
                node_state_dir=self.node_state_dir,
            )
        }
        for authority in ("control", "catalog", "users", "auth_sessions"):
            self.assertEqual(checks[authority]["status"], "UNAVAILABLE")
        self.assertEqual(checks["nodes"]["status"], "OK")
        self.assertEqual(checks["legacy_bootstrap_tokens"]["status"], "OK")

    def test_symlinked_node_root_is_rejected_without_following(self) -> None:
        real_node_state_dir = self.root / "node-real"
        self.node_state_dir.rename(real_node_state_dir)
        self.node_state_dir.symlink_to(real_node_state_dir, target_is_directory=True)

        with self.assertRaises(StateReadinessError):
            check_state_readiness(
                state_dir=self.state_dir,
                node_state_dir=self.node_state_dir,
            )

        checks = {
            check["authority"]: check
            for check in inspect_state_readiness(
                state_dir=self.state_dir,
                node_state_dir=self.node_state_dir,
            )
        }
        for authority in ("control", "catalog", "users", "auth_sessions"):
            self.assertEqual(checks[authority]["status"], "OK")
        self.assertEqual(checks["nodes"]["status"], "UNAVAILABLE")
        self.assertEqual(
            checks["legacy_bootstrap_tokens"]["status"], "UNAVAILABLE"
        )

    def test_inspect_cli_redacts_symlink_root_target_and_paths(self) -> None:
        real_state_dir = self.root / "AUDIT_DUMMY_ROOT_TARGET"
        self.state_dir.rename(real_state_dir)
        self.state_dir.symlink_to(real_state_dir, target_is_directory=True)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = inspect_main(
                [
                    "--state-dir",
                    str(self.state_dir),
                    "--node-state-dir",
                    str(self.node_state_dir),
                ]
            )

        rendered = output.getvalue()
        payload = json.loads(rendered)
        self.assertEqual(result, 2)
        self.assertEqual(payload["status"], "UNAVAILABLE")
        self.assertNotIn("AUDIT_DUMMY_ROOT_TARGET", rendered)
        self.assertNotIn(str(self.root), rendered)


if __name__ == "__main__":
    unittest.main()
