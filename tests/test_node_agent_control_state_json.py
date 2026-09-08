from __future__ import annotations

import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "node-agent"))

from agent import NodeAgent  # noqa: E402
from supervisor import FakeSupervisor  # noqa: E402


class NodeAgentControlStateJsonTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.control_path = root / "state" / "control.json"
        self.agent = NodeAgent(
            control_base_url="http://127.0.0.1:1",
            bootstrap_token="test-bootstrap-token",
            provider_server_id="provider-test",
            boot_id="boot-test",
            agent_version="test",
            secret_dir=root / "secrets",
            supervisor=FakeSupervisor(),
            control_state_path=self.control_path,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_seed_control_state_writes_validated_authority(self) -> None:
        command_id = str(uuid.uuid4())
        self.agent.seed_control_state(
            {
                "audio_mode": "MUTED",
                "audio_version": 2,
                "audio_command_id": command_id,
                "audio_idempotency_key": "request-1",
                "audio_updated_at": 123.5,
            }
        )

        state = json.loads(self.control_path.read_text(encoding="utf-8"))
        self.assertEqual(
            state,
            {
                "audio_mode": "MUTED",
                "version": 2,
                "command_id": command_id,
                "idempotency_key": "request-1",
                "updated_at": 123.5,
            },
        )

    def test_seed_control_state_rejects_invalid_control_fields_before_write(self) -> None:
        cases = {
            "boolean version": {"audio_version": True},
            "negative version": {"audio_version": -1},
            "invalid command id": {"audio_command_id": "not-a-uuid"},
            "oversized idempotency key": {"audio_idempotency_key": "x" * 201},
            "boolean update time": {"audio_updated_at": True},
            "nan update time": {"audio_updated_at": float("nan")},
            "positive infinity update time": {"audio_updated_at": float("inf")},
            "negative infinity update time": {"audio_updated_at": float("-inf")},
            "overflowing update time": {"audio_updated_at": 10**1000},
        }

        for name, mutation in cases.items():
            with self.subTest(case=name):
                if self.control_path.exists():
                    self.control_path.unlink()
                response: dict[str, object] = {
                    "audio_mode": "LIVE",
                    "audio_version": 0,
                    "audio_command_id": None,
                    "audio_idempotency_key": None,
                    "audio_updated_at": 1.0,
                }
                response.update(mutation)

                with self.assertRaises(RuntimeError):
                    self.agent.seed_control_state(response)

                self.assertFalse(self.control_path.exists())

    def test_serializer_failure_does_not_publish_control_authority(self) -> None:
        with patch("agent.json.dump", side_effect=TypeError("synthetic")):
            with self.assertRaisesRegex(RuntimeError, "cannot be serialized"):
                self.agent.seed_control_state(
                    {
                        "audio_mode": "LIVE",
                        "audio_version": 0,
                        "audio_updated_at": 1.0,
                    }
                )

        self.assertFalse(self.control_path.exists())
        self.assertEqual(list(self.control_path.parent.glob(".control.json.*")), [])


if __name__ == "__main__":
    unittest.main()
