from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from session_store import SessionStore  # noqa: E402


class SessionRecoveryStabilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SessionStore(self.tmp.name, recovery_stable_seconds=3.0)
        self.session_id = "session-recovery-stability"
        self.store.create(
            session_id=self.session_id,
            user_id="user-1",
            environment="dev",
            absolute_deadline_hours=12.0,
        )
        self.store.transition(self.session_id, "PROVISIONING")
        self.store.transition(self.session_id, "BOOTSTRAPPING")
        self.store.transition(self.session_id, "READY_WAIT_INGEST")
        self.apply("ACCEPTED", source_id="source-1", occurred_at=100.0)
        self.apply("OFFLINE", online=False, source_id=None, occurred_at=110.0)
        self.store.update(self.session_id, hold_deadline_at=200.0)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def observation(
        status: str,
        *,
        online: bool = True,
        source_id: str | None = "source-2",
        reasons: list[str] | None = None,
    ) -> dict[str, object]:
        return {
            "status": status,
            "path": "live/input",
            "online": online,
            "source_type": "rtmpConn" if online else None,
            "source_id": source_id if online else None,
            "bitrate_bps": 1_500_000 if online else None,
            "max_bitrate_bps": 6_000_000,
            "tracks": (
                [
                    {"codec": "H264", "width": 1280, "height": 720},
                    {
                        "codec": "MPEG-4 Audio",
                        "sampleRate": 48000,
                        "channelCount": 2,
                    },
                ]
                if online
                else []
            ),
            "quality": {"video_fps": 30.0} if online else None,
            "reasons": reasons or [],
            "warnings": [],
            "enforced": False,
            "observed_at": 100.0,
        }

    def apply(
        self,
        status: str,
        *,
        online: bool = True,
        source_id: str | None = "source-2",
        reasons: list[str] | None = None,
        event_types: list[str] | None = None,
        occurred_at: float,
    ) -> dict[str, object]:
        return self.store.apply_ingest_observation(
            self.session_id,
            node_id="node-1",
            event_types=event_types or [],
            observation=self.observation(
                status,
                online=online,
                source_id=source_id,
                reasons=reasons,
            ),
            occurred_at=occurred_at,
        )

    def test_healthy_reconnect_stays_holding_until_full_stability_window(self) -> None:
        first = self.apply("ACCEPTED", occurred_at=120.0)
        self.assertEqual(first["status"], "HOLDING")
        self.assertEqual(first["recovery_candidate_since"], 120.0)
        self.assertEqual(first["recovery_candidate_source_id"], "source-2")
        self.assertEqual(first["hold_deadline_at"], 200.0)

        almost = self.apply("ACCEPTED", occurred_at=122.999)
        self.assertEqual(almost["status"], "HOLDING")
        self.assertFalse(any(event["type"] == "session.recovered" for event in almost["events"][-1:]))

        recovered = self.apply("ACCEPTED", occurred_at=123.0)
        self.assertEqual(recovered["status"], "LIVE")
        self.assertEqual(recovered["events"][-1]["type"], "session.recovered")
        self.assertEqual(recovered["events"][-1]["payload"]["from_state"], "HOLDING")
        self.assertEqual(recovered["events"][-1]["payload"]["to_state"], "LIVE")
        self.assertIsNone(recovered["hold_deadline_at"])
        self.assertIsNone(recovered["recovery_candidate_since"])
        self.assertIsNone(recovered["recovery_candidate_source_id"])

    def test_recoverable_degraded_input_uses_same_stability_window(self) -> None:
        first = self.apply(
            "DEGRADED",
            reasons=["FPS_OUT_OF_RANGE"],
            occurred_at=120.0,
        )
        self.assertEqual(first["status"], "HOLDING")

        recovered = self.apply(
            "DEGRADED",
            reasons=["FPS_OUT_OF_RANGE"],
            occurred_at=123.0,
        )
        self.assertEqual(recovered["status"], "DEGRADED")
        self.assertEqual(recovered["events"][-1]["type"], "session.recovered")
        self.assertEqual(recovered["events"][-1]["reason_code"], "FPS_OUT_OF_RANGE")

    def test_unusable_observation_resets_candidate(self) -> None:
        self.apply("ACCEPTED", occurred_at=120.0)
        reset = self.apply(
            "DEGRADED",
            reasons=["VIDEO_TIMEOUT"],
            occurred_at=122.0,
        )
        self.assertEqual(reset["status"], "HOLDING")
        self.assertIsNone(reset["recovery_candidate_since"])

        restarted = self.apply("ACCEPTED", occurred_at=123.0)
        self.assertEqual(restarted["status"], "HOLDING")
        self.assertEqual(restarted["recovery_candidate_since"], 123.0)
        still_holding = self.apply("ACCEPTED", occurred_at=125.9)
        self.assertEqual(still_holding["status"], "HOLDING")
        recovered = self.apply("ACCEPTED", occurred_at=126.0)
        self.assertEqual(recovered["status"], "LIVE")

    def test_source_change_restarts_stability_window(self) -> None:
        self.apply("ACCEPTED", source_id="source-2", occurred_at=120.0)
        changed = self.apply("ACCEPTED", source_id="source-3", occurred_at=122.0)
        self.assertEqual(changed["status"], "HOLDING")
        self.assertEqual(changed["recovery_candidate_since"], 122.0)
        self.assertEqual(changed["recovery_candidate_source_id"], "source-3")

        still_holding = self.apply("ACCEPTED", source_id="source-3", occurred_at=124.9)
        self.assertEqual(still_holding["status"], "HOLDING")
        recovered = self.apply("ACCEPTED", source_id="source-3", occurred_at=125.0)
        self.assertEqual(recovered["status"], "LIVE")

    def test_candidate_survives_control_plane_store_restart(self) -> None:
        candidate = self.apply("ACCEPTED", occurred_at=120.0)
        self.assertEqual(candidate["status"], "HOLDING")

        self.store = SessionStore(self.tmp.name, recovery_stable_seconds=3.0)
        persisted = self.store.get(self.session_id)
        assert persisted is not None
        self.assertEqual(persisted["recovery_candidate_since"], 120.0)

        recovered = self.apply("ACCEPTED", occurred_at=123.0)
        self.assertEqual(recovered["status"], "LIVE")
        self.assertEqual(recovered["events"][-1]["type"], "session.recovered")

    def test_stop_during_stability_window_wins_over_late_recovery(self) -> None:
        candidate = self.apply("ACCEPTED", occurred_at=120.0)
        self.assertEqual(candidate["status"], "HOLDING")

        stopped = self.store.transition(self.session_id, "STOPPING")
        self.assertIsNone(stopped["recovery_candidate_since"])
        late = self.apply("ACCEPTED", occurred_at=124.0)
        self.assertEqual(late["status"], "STOPPING")
        self.assertFalse(any(event["type"] == "session.recovered" for event in late["events"][-1:]))


if __name__ == "__main__":
    unittest.main()
