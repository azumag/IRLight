from __future__ import annotations

import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "node-agent"))

from media_control_heartbeat import (  # noqa: E402
    add_audio_observation,
    apply_audio_command,
    bootstrap_audio_command,
)
from media_control_state import MediaControlStateError  # noqa: E402


class MediaControlHeartbeatAdapterTest(unittest.TestCase):
    def test_missing_status_does_not_emit_ack(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            payload = {"status": "READY"}
            self.assertEqual(
                add_audio_observation(
                    payload,
                    status_path=Path(root) / "status.json",
                ),
                payload,
            )

    def test_status_observation_is_nested_under_media_control_audio(self) -> None:
        command_id = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as root:
            status_path = Path(root) / "status.json"
            status_path.write_text(
                json.dumps(
                    {
                        "actual_audio_mode": "MUTED",
                        "control_version": 5,
                        "command_id": command_id,
                        "updated_at": 20.0,
                    }
                ),
                encoding="utf-8",
            )
            enriched = add_audio_observation(
                {"status": "READY"}, status_path=status_path
            )
        self.assertEqual(
            enriched["media_control"],
            {
                "audio": {
                    "actual_mode": "MUTED",
                    "control_version": 5,
                    "command_id": command_id,
                    "observed_at": 20.0,
                }
            },
        )

    def test_heartbeat_response_applies_newer_command(self) -> None:
        command_id = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as root:
            control_path = Path(root) / "control.json"
            result = apply_audio_command(
                {
                    "media_control": {
                        "audio": {
                            "desired_mode": "MUTED",
                            "version": 2,
                            "command_id": command_id,
                            "idempotency_key": "mute-2",
                            "updated_at": 30.0,
                        }
                    }
                },
                control_path=control_path,
            )
            stored = json.loads(control_path.read_text(encoding="utf-8"))
        self.assertEqual(result, "applied")
        self.assertEqual(stored["audio_mode"], "MUTED")
        self.assertEqual(stored["version"], 2)
        self.assertEqual(stored["command_id"], command_id)

    def test_missing_command_keeps_mixed_version_compatibility(self) -> None:
        self.assertIsNone(
            apply_audio_command({"desired_state": "RUNNING"}, control_path=Path("unused"))
        )

    def test_malformed_nested_response_fails_closed(self) -> None:
        with self.assertRaises(MediaControlStateError):
            apply_audio_command(
                {"media_control": "invalid"},  # type: ignore[dict-item]
                control_path=Path("unused"),
            )

    def test_bootstrap_prefers_nested_command(self) -> None:
        command_id = str(uuid.uuid4())
        command = {
            "desired_mode": "MUTED",
            "version": 4,
            "command_id": command_id,
            "idempotency_key": "mute-4",
            "updated_at": 40.0,
        }
        self.assertEqual(
            bootstrap_audio_command({"media_control": {"audio": command}}),
            command,
        )

    def test_bootstrap_translates_flat_rolling_upgrade_fields(self) -> None:
        command_id = str(uuid.uuid4())
        self.assertEqual(
            bootstrap_audio_command(
                {
                    "audio_mode": "LIVE",
                    "audio_version": 8,
                    "audio_command_id": command_id,
                    "audio_idempotency_key": "live-8",
                    "audio_updated_at": 50.0,
                }
            ),
            {
                "desired_mode": "LIVE",
                "version": 8,
                "command_id": command_id,
                "idempotency_key": "live-8",
                "updated_at": 50.0,
            },
        )


if __name__ == "__main__":
    unittest.main()
