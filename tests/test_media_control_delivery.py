from __future__ import annotations

import sys
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from media_control_delivery import (  # noqa: E402
    MediaControlDeliveryError,
    classify_audio_observation,
    heartbeat_audio_observation,
    heartbeat_media_control_response,
    normalize_audio_observation,
    session_audio_command,
)


class MediaControlDeliveryTest(unittest.TestCase):
    @staticmethod
    def _session(
        *,
        desired: str = "MUTED",
        version: int = 3,
        command_id: str | None = None,
    ) -> dict[str, object]:
        if command_id is None and version > 0:
            command_id = str(uuid.uuid4())
        return {
            "updated_at": 10.0,
            "media_control": {
                "audio": {
                    "desired_mode": desired,
                    "actual_mode": "APPLYING",
                    "version": version,
                    "command_id": command_id,
                    "idempotency_key": "operator-command",
                    "updated_at": 11.0,
                }
            },
        }

    @staticmethod
    def _observation(
        *,
        mode: str = "MUTED",
        version: int = 3,
        command_id: str | None = None,
    ) -> dict[str, object]:
        if command_id is None and version > 0:
            command_id = str(uuid.uuid4())
        return {
            "actual_mode": mode,
            "control_version": version,
            "command_id": command_id,
            "observed_at": 12.0,
        }

    def test_session_command_preserves_final_state_authority(self) -> None:
        command_id = str(uuid.uuid4())
        command = session_audio_command(
            self._session(desired="LIVE", version=4, command_id=command_id)
        )
        self.assertEqual(command["desired_mode"], "LIVE")
        self.assertEqual(command["version"], 4)
        self.assertEqual(command["command_id"], command_id)
        self.assertEqual(command["updated_at"], 11.0)

    def test_legacy_session_falls_back_to_live_version_zero(self) -> None:
        self.assertEqual(
            session_audio_command({"updated_at": 9.5}),
            {
                "desired_mode": "LIVE",
                "version": 0,
                "command_id": None,
                "idempotency_key": None,
                "updated_at": 9.5,
            },
        )

    def test_matching_version_and_command_is_accepted(self) -> None:
        command_id = str(uuid.uuid4())
        session = self._session(version=3, command_id=command_id)
        observation = self._observation(version=3, command_id=command_id)
        self.assertEqual(
            classify_audio_observation(session, observation),
            "matched",
        )

    def test_stale_observation_is_ignored(self) -> None:
        session = self._session(version=4)
        observation = self._observation(version=3)
        self.assertEqual(classify_audio_observation(session, observation), "stale")

    def test_future_observation_is_ignored(self) -> None:
        session = self._session(version=3)
        observation = self._observation(version=4)
        self.assertEqual(classify_audio_observation(session, observation), "future")

    def test_same_version_different_command_is_not_accepted(self) -> None:
        session = self._session(version=3, command_id=str(uuid.uuid4()))
        observation = self._observation(version=3, command_id=str(uuid.uuid4()))
        self.assertEqual(
            classify_audio_observation(session, observation),
            "command_mismatch",
        )

    def test_initial_version_zero_matches_without_command_id(self) -> None:
        session = self._session(desired="LIVE", version=0, command_id=None)
        session["media_control"]["audio"]["idempotency_key"] = None  # type: ignore[index]
        observation = self._observation(mode="LIVE", version=0, command_id=None)
        self.assertEqual(
            classify_audio_observation(session, observation),
            "matched",
        )

    def test_observation_rejects_non_runtime_actual_mode(self) -> None:
        observation = self._observation()
        observation["actual_mode"] = "APPLYING"
        with self.assertRaises(MediaControlDeliveryError):
            normalize_audio_observation(observation)

    def test_versioned_observation_requires_command_uuid(self) -> None:
        observation = self._observation()
        observation["command_id"] = None
        with self.assertRaises(MediaControlDeliveryError):
            normalize_audio_observation(observation)

    def test_heartbeat_response_wraps_current_command(self) -> None:
        command_id = str(uuid.uuid4())
        session = self._session(version=6, command_id=command_id)
        self.assertEqual(
            heartbeat_media_control_response(session),
            {
                "audio": {
                    "desired_mode": "MUTED",
                    "version": 6,
                    "command_id": command_id,
                    "idempotency_key": "operator-command",
                    "updated_at": 11.0,
                }
            },
        )

    def test_missing_heartbeat_media_control_is_rolling_upgrade_compatible(self) -> None:
        self.assertIsNone(heartbeat_audio_observation(None))
        self.assertIsNone(heartbeat_audio_observation({}))

    def test_heartbeat_observation_envelope_is_strict(self) -> None:
        command_id = str(uuid.uuid4())
        observation = self._observation(version=7, command_id=command_id)
        self.assertEqual(
            heartbeat_audio_observation({"audio": observation}),
            observation,
        )
        with self.assertRaises(MediaControlDeliveryError):
            heartbeat_audio_observation("invalid")


if __name__ == "__main__":
    unittest.main()
