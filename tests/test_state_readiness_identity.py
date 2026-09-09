from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

import state_readiness  # noqa: E402
from auth_store import _default_sessions, _default_users  # noqa: E402
from control_store import default_control  # noqa: E402
from state_readiness import StateReadinessError, check_state_readiness  # noqa: E402
from state_safety import initialization_marker  # noqa: E402


class StateReadinessEntryIdentityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="irlight-readyz-entry-")
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

    @staticmethod
    def _atomic_replace(path: Path, content: bytes) -> None:
        replacement = path.with_name(path.name + ".replacement")
        replacement.write_bytes(content)
        os.replace(replacement, path)

    def _check(self) -> None:
        check_state_readiness(
            state_dir=self.state_dir,
            node_state_dir=self.node_state_dir,
        )

    def test_authority_replaced_during_validation_is_rejected(self) -> None:
        authority_path = self.state_dir / "control.json"
        original_validator = state_readiness._validate_control
        original_bytes = authority_path.read_bytes()

        def replace_after_validation(value: dict[str, object]) -> object:
            result = original_validator(value)
            self._atomic_replace(authority_path, original_bytes)
            return result

        with mock.patch.object(
            state_readiness, "_validate_control", side_effect=replace_after_validation
        ):
            with self.assertRaisesRegex(
                StateReadinessError, "required state entry changed during inspection"
            ):
                self._check()

        self.assertEqual(authority_path.read_bytes(), original_bytes)

    def test_marker_replaced_during_validation_is_rejected(self) -> None:
        authority_path = self.state_dir / "control.json"
        marker_path = initialization_marker(authority_path)
        original_validator = state_readiness._validate_control
        marker_bytes = marker_path.read_bytes()

        def replace_marker_after_validation(value: dict[str, object]) -> object:
            result = original_validator(value)
            self._atomic_replace(marker_path, marker_bytes)
            return result

        with mock.patch.object(
            state_readiness,
            "_validate_control",
            side_effect=replace_marker_after_validation,
        ):
            with self.assertRaisesRegex(
                StateReadinessError, "required state entry changed during inspection"
            ):
                self._check()

        self.assertEqual(marker_path.read_bytes(), marker_bytes)


if __name__ == "__main__":
    unittest.main()
