from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from session_store import SessionStore  # noqa: E402


class ContinuityLifecycleGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SessionStore(self.tmp.name)
        session = self.store.create(user_id="user-1", environment="dev")
        self.session_id = str(session["session_id"])
        self.store.transition(self.session_id, "PROVISIONING")
        self.store.transition(self.session_id, "BOOTSTRAPPING")
        self.store.transition(self.session_id, "READY_WAIT_INGEST")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def _observation(status: str, *, online: bool = True, reason: str | None = None) -> dict[str, object]:
        return {
            "status": status,
            "path": "live/input",
            "online": online,
            "source_type": "rtmpConn" if online else None,
            "source_id": "source-1" if online else None,
            "bitrate_bps": 1_500_000 if online else None,
            "max_bitrate_bps": 6_000_000,
            "tracks": [],
            "quality": None,
            "reasons": [reason] if reason else [],
            "warnings": ["CONTINUITY_STABILIZING"] if status == "PENDING" else [],
            "enforced": False,
            "observed_at": 100.0,
        }

    def test_initial_online_input_waits_in_ready_until_media_switch_is_live(self) -> None:
        pending = self.store.apply_ingest_observation(
            self.session_id,
            node_id="node-1",
            event_types=["ingest.connected", "ingest.format_detected"],
            observation=self._observation("PENDING"),
            occurred_at=100.0,
        )
        self.assertEqual(pending["status"], "READY_WAIT_INGEST")
        self.assertEqual(
            [event["type"] for event in pending["events"]],
            ["ingest.connected", "ingest.format_detected"],
        )
        self.assertFalse(any(event["type"] == "session.live" for event in pending["events"]))

        live = self.store.apply_ingest_observation(
            self.session_id,
            node_id="node-1",
            event_types=["ingest.policy_changed"],
            observation=self._observation("ACCEPTED"),
            occurred_at=104.0,
        )
        self.assertEqual(live["status"], "LIVE")
        self.assertEqual(live["events"][-1]["type"], "session.live")
        self.assertEqual(live["events"][-1]["payload"]["from_state"], "READY_WAIT_INGEST")
        self.assertEqual(live["events"][-1]["payload"]["to_state"], "LIVE")

    def test_reconnect_waits_in_holding_then_can_recover_degraded(self) -> None:
        self.store.apply_ingest_observation(
            self.session_id,
            node_id="node-1",
            event_types=["ingest.connected", "ingest.format_detected"],
            observation=self._observation("ACCEPTED"),
            occurred_at=90.0,
        )
        holding = self.store.apply_ingest_observation(
            self.session_id,
            node_id="node-1",
            event_types=["ingest.disconnected"],
            observation=self._observation("OFFLINE", online=False),
            occurred_at=95.0,
        )
        self.assertEqual(holding["status"], "HOLDING")

        pending = self.store.apply_ingest_observation(
            self.session_id,
            node_id="node-1",
            event_types=["ingest.reconnected", "ingest.format_detected"],
            observation=self._observation("PENDING"),
            occurred_at=100.0,
        )
        self.assertEqual(pending["status"], "HOLDING")
        self.assertEqual(pending["events"][-1]["type"], "ingest.format_detected")

        recovered = self.store.apply_ingest_observation(
            self.session_id,
            node_id="node-1",
            event_types=["ingest.degraded"],
            observation=self._observation("DEGRADED", reason="BITRATE_TOO_LOW"),
            occurred_at=104.0,
        )
        self.assertEqual(recovered["status"], "DEGRADED")
        self.assertEqual(recovered["events"][-1]["type"], "session.recovered")
        self.assertEqual(recovered["events"][-1]["reason_code"], "BITRATE_TOO_LOW")
        self.assertEqual(recovered["events"][-1]["payload"]["from_state"], "HOLDING")
        self.assertEqual(recovered["events"][-1]["payload"]["to_state"], "DEGRADED")


if __name__ == "__main__":
    unittest.main()
