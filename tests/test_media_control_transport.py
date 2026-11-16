from __future__ import annotations

import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "node-agent"))

from media_control_transport import media_control_http_json  # noqa: E402


class MediaControlTransportTest(unittest.TestCase):
    def test_heartbeat_attaches_observation_and_applies_response_command(self) -> None:
        observed_id = str(uuid.uuid4())
        next_id = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            status_path = state / "status.json"
            control_path = state / "control.json"
            status_path.write_text(
                json.dumps(
                    {
                        "actual_audio_mode": "LIVE",
                        "control_version": 1,
                        "command_id": observed_id,
                        "updated_at": 10.0,
                    }
                ),
                encoding="utf-8",
            )
            seen: list[dict[str, object] | None] = []

            def delegate(url: str, **kwargs: object) -> dict[str, object]:
                seen.append(kwargs.get("payload"))  # type: ignore[arg-type]
                return {
                    "desired_state": "RUNNING",
                    "media_control": {
                        "audio": {
                            "desired_mode": "MUTED",
                            "version": 2,
                            "command_id": next_id,
                            "idempotency_key": "mute-2",
                            "updated_at": 11.0,
                        }
                    },
                }

            wrapped = media_control_http_json(
                delegate, status_path=status_path, control_path=control_path
            )
            response = wrapped(
                "https://control/internal/nodes/node-1/heartbeat",
                method="POST",
                token="token",
                payload={"status": "READY"},
            )
            stored = json.loads(control_path.read_text(encoding="utf-8"))

        self.assertEqual(response["desired_state"], "RUNNING")
        assert seen[0] is not None
        self.assertEqual(
            seen[0]["media_control"],
            {
                "audio": {
                    "actual_mode": "LIVE",
                    "control_version": 1,
                    "command_id": observed_id,
                    "observed_at": 10.0,
                }
            },
        )
        self.assertEqual(stored["audio_mode"], "MUTED")
        self.assertEqual(stored["version"], 2)
        self.assertEqual(stored["command_id"], next_id)

    def test_bad_local_status_does_not_block_lifecycle_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            status_path = state / "status.json"
            control_path = state / "control.json"
            status_path.write_text("not json", encoding="utf-8")
            seen: list[dict[str, object] | None] = []

            def delegate(url: str, **kwargs: object) -> dict[str, object]:
                seen.append(kwargs.get("payload"))  # type: ignore[arg-type]
                return {"desired_state": "STOPPED"}

            response = media_control_http_json(
                delegate, status_path=status_path, control_path=control_path
            )(
                "https://control/internal/nodes/node-1/heartbeat",
                method="POST",
                payload={"status": "READY"},
            )

        self.assertEqual(response["desired_state"], "STOPPED")
        self.assertEqual(seen, [{"status": "READY"}])

    def test_conflicting_remote_command_does_not_block_stop_response(self) -> None:
        command_id = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            control_path = state / "control.json"
            control_path.write_text(
                json.dumps(
                    {
                        "audio_mode": "LIVE",
                        "version": 3,
                        "command_id": command_id,
                        "idempotency_key": "live-3",
                        "updated_at": 10.0,
                    }
                ),
                encoding="utf-8",
            )

            def delegate(url: str, **kwargs: object) -> dict[str, object]:
                return {
                    "desired_state": "STOPPED",
                    "media_control": {
                        "audio": {
                            "desired_mode": "MUTED",
                            "version": 3,
                            "command_id": command_id,
                            "idempotency_key": "mute-3",
                            "updated_at": 11.0,
                        }
                    },
                }

            response = media_control_http_json(
                delegate,
                status_path=state / "missing-status.json",
                control_path=control_path,
            )(
                "https://control/internal/nodes/node-1/heartbeat",
                method="POST",
                payload={"status": "READY"},
            )
            stored = json.loads(control_path.read_text(encoding="utf-8"))

        self.assertEqual(response["desired_state"], "STOPPED")
        self.assertEqual(stored["audio_mode"], "LIVE")
        self.assertEqual(stored["version"], 3)

    def test_bootstrap_nested_command_is_translated_for_existing_agent_seed(self) -> None:
        command_id = str(uuid.uuid4())

        def delegate(url: str, **kwargs: object) -> dict[str, object]:
            return {
                "node_id": "node-1",
                "session_id": "session-1",
                "media_control": {
                    "audio": {
                        "desired_mode": "MUTED",
                        "version": 4,
                        "command_id": command_id,
                        "idempotency_key": "mute-4",
                        "updated_at": 20.0,
                    }
                },
            }

        response = media_control_http_json(
            delegate,
            status_path=Path("unused-status"),
            control_path=Path("unused-control"),
        )(
            "https://control/internal/nodes/bootstrap",
            method="POST",
            payload={"provider_server_id": "srv"},
        )
        self.assertEqual(response["audio_mode"], "MUTED")
        self.assertEqual(response["audio_version"], 4)
        self.assertEqual(response["audio_command_id"], command_id)


if __name__ == "__main__":
    unittest.main()
