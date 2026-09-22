from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "node-agent"))

from agent import NodeAgent  # noqa: E402
from supervisor import FakeSupervisor  # noqa: E402


class FailingIngestInspector:
    def observe_and_enforce(self) -> dict[str, object]:
        raise RuntimeError("MediaMTX API unavailable")


class NodeAgentIngestFallbackClockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.agent = NodeAgent(
            control_base_url="http://127.0.0.1:1",
            bootstrap_token="test-bootstrap-token",
            provider_server_id="provider-test",
            boot_id="boot-test",
            agent_version="test",
            secret_dir=Path(self.tmp.name) / "secrets",
            supervisor=FakeSupervisor(),
            ingest_inspector=FailingIngestInspector(),  # type: ignore[arg-type]
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_valid_clock_preserves_mediamtx_unavailable_fallback(self) -> None:
        with patch("agent.time.time", return_value=123.0):
            observation = self.agent._ingest_observation()

        self.assertIsNotNone(observation)
        assert observation is not None
        self.assertEqual(observation["status"], "UNKNOWN")
        self.assertEqual(observation["reasons"], ["MEDIAMTX_API_UNAVAILABLE"])
        self.assertEqual(observation["observed_at"], 123.0)

    def test_epoch_zero_is_valid_for_fallback_observation(self) -> None:
        with patch("agent.time.time", return_value=0.0):
            observation = self.agent._ingest_observation()

        self.assertIsNotNone(observation)
        assert observation is not None
        self.assertEqual(observation["observed_at"], 0.0)

    def test_invalid_fallback_clock_fails_closed(self) -> None:
        invalid_values: list[object] = [
            -1.0,
            float("nan"),
            float("inf"),
            float("-inf"),
            True,
            "100",
            10**400,
        ]
        for invalid in invalid_values:
            with self.subTest(invalid=invalid), patch(
                "agent.time.time", return_value=invalid
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "ingest observation clock is invalid"
                ):
                    self.agent._ingest_observation()

    def test_invalid_fallback_clock_is_not_sent_to_control_plane(self) -> None:
        self.agent.node_id = "node-test"
        self.agent.node_access_token = "node-access-token"
        self.agent.absolute_deadline = None

        with patch("agent.time.time", return_value=-1.0), patch(
            "agent.http_json"
        ) as http_json:
            with self.assertRaisesRegex(
                RuntimeError, "ingest observation clock is invalid"
            ):
                self.agent.heartbeat()

        http_json.assert_not_called()


if __name__ == "__main__":
    unittest.main()
