from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "node-agent"))

from media_control_state import (  # noqa: E402
    MediaControlStateConflict,
    MediaControlStateError,
    read_audio_observation,
    reconcile_audio_command,
)


class MediaControlStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="irlight-media-control-")
        self.root = Path(self.temporary.name)
        self.control_path = self.root / "control.json"
        self.status_path = self.root / "status.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _command(
        *,
        mode: str = "MUTED",
        version: int = 1,
        command_id: str | None = None,
        updated_at: float = 100.0,
    ) -> dict[str, object]:
        if command_id is None and version > 0:
            command_id = str(uuid.uuid4())
        return {
            "desired_mode": mode,
            "version": version,
            "command_id": command_id,
            "idempotency_key": "operator-command-1" if version > 0 else None,
            "updated_at": updated_at,
        }

    def _read_control(self) -> dict[str, object]:
        return json.loads(self.control_path.read_text(encoding="utf-8"))

    def test_missing_local_state_accepts_remote_authority(self) -> None:
        command = self._command()
        self.assertEqual(
            reconcile_audio_command(self.control_path, command),
            "applied",
        )
        stored = self._read_control()
        self.assertEqual(stored["audio_mode"], "MUTED")
        self.assertEqual(stored["version"], 1)
        self.assertEqual(stored["command_id"], command["command_id"])
        self.assertEqual(os.stat(self.control_path).st_mode & 0o777, 0o600)

    def test_newer_remote_version_replaces_local_state(self) -> None:
        first_id = str(uuid.uuid4())
        second_id = str(uuid.uuid4())
        reconcile_audio_command(
            self.control_path,
            self._command(mode="MUTED", version=1, command_id=first_id),
        )
        result = reconcile_audio_command(
            self.control_path,
            self._command(mode="LIVE", version=2, command_id=second_id, updated_at=101.0),
        )
        self.assertEqual(result, "applied")
        stored = self._read_control()
        self.assertEqual(stored["audio_mode"], "LIVE")
        self.assertEqual(stored["version"], 2)
        self.assertEqual(stored["command_id"], second_id)

    def test_identical_version_and_command_is_idempotent(self) -> None:
        command_id = str(uuid.uuid4())
        reconcile_audio_command(
            self.control_path,
            self._command(command_id=command_id),
        )
        before = self.control_path.read_bytes()
        result = reconcile_audio_command(
            self.control_path,
            self._command(command_id=command_id, updated_at=999.0),
        )
        self.assertEqual(result, "noop")
        self.assertEqual(self.control_path.read_bytes(), before)

    def test_stale_remote_version_never_rolls_local_state_back(self) -> None:
        current_id = str(uuid.uuid4())
        reconcile_audio_command(
            self.control_path,
            self._command(mode="LIVE", version=3, command_id=current_id),
        )
        before = self.control_path.read_bytes()
        result = reconcile_audio_command(
            self.control_path,
            self._command(mode="MUTED", version=2, updated_at=101.0),
        )
        self.assertEqual(result, "stale")
        self.assertEqual(self.control_path.read_bytes(), before)

    def test_same_version_with_different_authority_fails_closed(self) -> None:
        command_id = str(uuid.uuid4())
        reconcile_audio_command(
            self.control_path,
            self._command(mode="LIVE", version=2, command_id=command_id),
        )
        before = self.control_path.read_bytes()
        with self.assertRaises(MediaControlStateConflict):
            reconcile_audio_command(
                self.control_path,
                self._command(mode="MUTED", version=2, command_id=command_id),
            )
        self.assertEqual(self.control_path.read_bytes(), before)

    def test_versioned_command_requires_uuid(self) -> None:
        command = self._command()
        command["command_id"] = "not-a-uuid"
        with self.assertRaises(MediaControlStateError):
            reconcile_audio_command(self.control_path, command)
        self.assertFalse(self.control_path.exists())

    def test_missing_status_has_no_ack(self) -> None:
        self.assertIsNone(read_audio_observation(self.status_path))

    def test_status_is_converted_to_safe_ack_shape(self) -> None:
        command_id = str(uuid.uuid4())
        self.status_path.write_text(
            json.dumps(
                {
                    "actual_audio_mode": "MUTED",
                    "control_version": 4,
                    "command_id": command_id,
                    "updated_at": 123.5,
                    "egress": "redacted",
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(
            read_audio_observation(self.status_path),
            {
                "actual_mode": "MUTED",
                "control_version": 4,
                "command_id": command_id,
                "observed_at": 123.5,
            },
        )

    def test_malformed_status_is_never_reported_as_applied(self) -> None:
        self.status_path.write_text(
            json.dumps(
                {
                    "actual_audio_mode": "MUTED",
                    "control_version": 5,
                    "command_id": None,
                    "updated_at": 123.5,
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(MediaControlStateError):
            read_audio_observation(self.status_path)


if __name__ == "__main__":
    unittest.main()
