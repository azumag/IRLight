from __future__ import annotations

import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "node-agent"))

from agent import DeadlineClockError, NodeAgent  # noqa: E402
from supervisor import FakeSupervisor  # noqa: E402


INVALID_CLOCK_CASES: tuple[tuple[str, object], ...] = (
    ("bool", True),
    ("string", "100"),
    ("negative", -1),
    ("nan", math.nan),
    ("positive-infinity", math.inf),
    ("negative-infinity", -math.inf),
    ("float-overflow", 10**10000),
)


class NodeAgentDeadlineClockTest(unittest.TestCase):
    def _agent(self) -> NodeAgent:
        return NodeAgent(
            control_base_url="http://control.invalid",
            bootstrap_token="bootstrap-token",
            provider_server_id="conoha-test-1",
            boot_id="boot-test",
            agent_version="test",
            secret_dir=Path(tempfile.mkdtemp(prefix="irlight-deadline-clock-")),
            supervisor=FakeSupervisor(),
            heartbeat_interval=0.01,
        )

    @staticmethod
    def _bootstrap_response(agent: NodeAgent, deadline: object = 100.0) -> dict[str, object]:
        return {
            "node_id": "node-test",
            "session_id": "session-test",
            "absolute_deadline": deadline,
            "egress_mode": "RELAY_ONLY",
            "node_access_token": agent.node_access_token or "",
        }

    def test_bootstrap_rejects_invalid_present_absolute_deadline(self) -> None:
        for label, value in INVALID_CLOCK_CASES:
            with self.subTest(case=label):
                agent = self._agent()
                response = self._bootstrap_response(agent, value)
                with patch("agent.http_json", return_value=response):
                    with self.assertRaisesRegex(
                        DeadlineClockError,
                        "invalid absolute deadline",
                    ):
                        agent.bootstrap()

    def test_bootstrap_keeps_missing_deadline_optional_and_epoch_zero_valid(self) -> None:
        no_deadline_agent = self._agent()
        no_deadline = self._bootstrap_response(no_deadline_agent)
        no_deadline.pop("absolute_deadline")
        with patch("agent.http_json", return_value=no_deadline):
            no_deadline_agent.bootstrap()
        self.assertIsNone(no_deadline_agent.absolute_deadline)

        epoch_agent = self._agent()
        with patch(
            "agent.http_json",
            return_value=self._bootstrap_response(epoch_agent, 0.0),
        ):
            epoch_agent.bootstrap()
        self.assertEqual(epoch_agent.absolute_deadline, 0.0)

    def test_invalid_pre_start_wall_clock_prevents_media_start(self) -> None:
        for label, clock_value in INVALID_CLOCK_CASES:
            with self.subTest(case=label):
                agent = self._agent()
                response = self._bootstrap_response(agent, 100.0)
                with patch("agent.http_json", return_value=response), patch(
                    "agent.time.time",
                    return_value=clock_value,
                ):
                    self.assertEqual(agent.run(), 1)
                self.assertEqual(agent.supervisor.started_sessions, [])

    def test_invalid_heartbeat_wall_clock_prevents_publish(self) -> None:
        for label, clock_value in INVALID_CLOCK_CASES:
            with self.subTest(case=label):
                agent = self._agent()
                agent.node_id = "node-test"
                agent.absolute_deadline = 100.0
                with patch("agent.time.time", return_value=clock_value), patch(
                    "agent.http_json"
                ) as http_json:
                    with self.assertRaises(DeadlineClockError):
                        agent.heartbeat()
                http_json.assert_not_called()

    def test_invalid_watchdog_wall_clock_stops_media(self) -> None:
        agent = self._agent()
        agent.absolute_deadline = 100.0
        agent.supervisor.start("session-test")

        with patch("agent.time.time", return_value=math.nan):
            agent._deadline_watchdog("session-test")

        self.assertTrue(agent._shutdown_event.is_set())
        self.assertTrue(agent._stop_requested)
        self.assertEqual(agent.supervisor.stopped_sessions, ["session-test"])

    def test_epoch_zero_runtime_clock_is_valid(self) -> None:
        agent = self._agent()
        agent.node_id = "node-test"
        agent.absolute_deadline = 1.0
        with patch("agent.time.time", return_value=0.0), patch(
            "agent.http_json",
            return_value={"desired_state": "STOPPED"},
        ) as http_json:
            response = agent.heartbeat()

        self.assertEqual(response["desired_state"], "STOPPED")
        payload = http_json.call_args.kwargs["payload"]
        self.assertEqual(payload["deadline_remaining_seconds"], 1.0)


if __name__ == "__main__":
    unittest.main()
