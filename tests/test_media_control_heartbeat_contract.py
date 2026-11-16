from __future__ import annotations

import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))
sys.path.insert(0, str(ROOT / "apps" / "node-agent"))

from media_control_delivery import (  # noqa: E402
    classify_audio_observation,
    session_audio_command,
)
from media_control_state import (  # noqa: E402
    read_audio_observation,
    reconcile_audio_command,
)


class MediaControlHeartbeatContractTest(unittest.TestCase):
    def test_command_round_trip_preserves_version_and_command_identity(self) -> None:
        command_id = str(uuid.uuid4())
        session = {
            "updated_at": 100.0,
            "media_control": {
                "audio": {
                    "desired_mode": "MUTED",
                    "actual_mode": "APPLYING",
                    "version": 7,
                    "command_id": command_id,
                    "idempotency_key": "mute-command",
                    "updated_at": 101.0,
                }
            },
        }

        with tempfile.TemporaryDirectory(prefix="irlight-media-contract-") as root:
            state_dir = Path(root)
            control_path = state_dir / "control.json"
            status_path = state_dir / "status.json"

            command = session_audio_command(session)
            self.assertEqual(reconcile_audio_command(control_path, command), "applied")
            local = json.loads(control_path.read_text(encoding="utf-8"))
            self.assertEqual(local["audio_mode"], "MUTED")
            self.assertEqual(local["version"], 7)
            self.assertEqual(local["command_id"], command_id)

            status_path.write_text(
                json.dumps(
                    {
                        "actual_audio_mode": "MUTED",
                        "control_version": local["version"],
                        "command_id": local["command_id"],
                        "updated_at": 102.0,
                    }
                ),
                encoding="utf-8",
            )
            observation = read_audio_observation(status_path)
            self.assertIsNotNone(observation)
            self.assertEqual(
                classify_audio_observation(session, observation),
                "matched",
            )

    def test_delayed_ack_for_previous_command_cannot_match_current_authority(self) -> None:
        old_id = str(uuid.uuid4())
        current_id = str(uuid.uuid4())
        current_session = {
            "updated_at": 200.0,
            "media_control": {
                "audio": {
                    "desired_mode": "LIVE",
                    "actual_mode": "APPLYING",
                    "version": 9,
                    "command_id": current_id,
                    "idempotency_key": "unmute-command",
                    "updated_at": 201.0,
                }
            },
        }
        delayed = {
            "actual_mode": "MUTED",
            "control_version": 8,
            "command_id": old_id,
            "observed_at": 202.0,
        }
        self.assertEqual(
            classify_audio_observation(current_session, delayed),
            "stale",
        )


if __name__ == "__main__":
    unittest.main()
