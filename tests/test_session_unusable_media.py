from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from session_store import SessionStore  # noqa: E402


class SessionUnusableMediaTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        # Recovery timing is covered separately; keep these tests focused on
        # unusable-media reason mapping and state-transition semantics.
        self.store = SessionStore(self.tmp.name, recovery_stable_seconds=0.0)
        self.session_id = "session-unusable-media"
        self.store.create(
            session_id=self.session_id,
            user_id="user-1",
            environment="dev",
            absolute_deadline_hours=12.0,
        )
        self.store.transition(self.session_id, "PROVISIONING")
        self.store.transition(
            self.session_id,
            "BOOTSTRAPPING",
            provider_server_id="provider-1",
        )
        self.store.transition(self.session_id, "READY_WAIT_INGEST")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def observation(
        status: str,
        *,
        reasons: list[str] | None = None,
        online: bool = True,
        source_id: str = "source-1",
    ) -> dict[str, object]:
        return {
            "status": status,
            "path": "live/input",
            "online": online,
            "source_type": "rtmpConn" if online else None,
            "source_id": source_id if online else None,
            "bitrate_bps": 1_500_000 if online else None,
            "max_bitrate_bps": 6_000_000,
            "tracks": [
                {"codec": "H264", "width": 1280, "height": 720},
                {
                    "codec": "MPEG-4 Audio",
                    "sampleRate": 48000,
                    "channelCount": 2,
                },
            ] if online else [],
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
        reasons: list[str] | None = None,
        event_types: list[str] | None = None,
        occurred_at: float = 100.0,
    ) -> dict[str, object]:
        return self.store.apply_ingest_observation(
            self.session_id,
            node_id="node-1",
            event_types=event_types or [],
            observation=self.observation(status, reasons=reasons),
            occurred_at=occurred_at,
        )

    def make_live(self) -> None:
        session = self.apply("ACCEPTED", event_types=["ingest.connected"])
        self.assertEqual(session["status"], "LIVE")

    def assert_unusable_timeout_holds(self, reason: str) -> None:
        self.make_live()
        session = self.apply(
            "DEGRADED",
            reasons=[reason],
            event_types=["ingest.degraded"],
            occurred_at=110.0,
        )
        self.assertEqual(session["status"], "HOLDING")
        self.assertEqual(session["events"][-1]["type"], "session.holding")
        self.assertEqual(session["events"][-1]["reason_code"], reason)
        self.assertEqual(session["events"][-1]["payload"]["from_state"], "LIVE")
        self.assertEqual(session["events"][-1]["payload"]["to_state"], "HOLDING")
        self.assertEqual(session["last_ingest_at"], 110.0)

        event_count = len(session["events"])
        session = self.apply(
            "DEGRADED",
            reasons=[reason],
            event_types=[],
            occurred_at=111.0,
        )
        self.assertEqual(session["status"], "HOLDING")
        self.assertEqual(len(session["events"]), event_count)

        session = self.apply("ACCEPTED", occurred_at=120.0)
        self.assertEqual(session["status"], "LIVE")
        self.assertEqual(session["events"][-1]["type"], "session.recovered")
        self.assertIsNone(session["hold_deadline_at"])

    def test_video_timeout_moves_running_session_to_holding(self) -> None:
        self.assert_unusable_timeout_holds("VIDEO_TIMEOUT")

    def test_audio_timeout_moves_running_session_to_holding(self) -> None:
        self.assert_unusable_timeout_holds("AUDIO_TIMEOUT")

    def test_nonfatal_quality_degradation_stays_degraded(self) -> None:
        self.make_live()
        session = self.apply(
            "DEGRADED",
            reasons=["FPS_OUT_OF_RANGE"],
            event_types=["ingest.degraded"],
        )
        self.assertEqual(session["status"], "DEGRADED")
        self.assertEqual(session["events"][-1]["type"], "session.degraded")
        self.assertEqual(session["events"][-1]["reason_code"], "FPS_OUT_OF_RANGE")

    def test_format_rejection_after_live_is_recorded_as_format_changed(self) -> None:
        self.make_live()
        session = self.apply(
            "REJECTED",
            reasons=["RESOLUTION_UNSUPPORTED"],
            event_types=["ingest.rejected"],
            occurred_at=130.0,
        )
        self.assertEqual(session["status"], "HOLDING")
        self.assertEqual(session["events"][-1]["type"], "session.holding")
        self.assertEqual(session["events"][-1]["reason_code"], "FORMAT_CHANGED")
        self.assertEqual(
            session["events"][-1]["payload"]["reasons"],
            ["RESOLUTION_UNSUPPORTED"],
        )

    def test_initial_rejected_format_does_not_start_holding(self) -> None:
        session = self.apply(
            "REJECTED",
            reasons=["VIDEO_CODEC_UNSUPPORTED"],
            event_types=["ingest.rejected"],
        )
        self.assertEqual(session["status"], "READY_WAIT_INGEST")
        self.assertFalse(any(event["type"] == "session.holding" for event in session["events"]))

    def test_nonformat_hard_rejection_keeps_specific_reason(self) -> None:
        self.make_live()
        session = self.apply(
            "REJECTED",
            reasons=["BITRATE_TOO_HIGH"],
            event_types=["ingest.rejected"],
        )
        self.assertEqual(session["status"], "HOLDING")
        self.assertEqual(session["events"][-1]["reason_code"], "BITRATE_TOO_HIGH")


if __name__ == "__main__":
    unittest.main()
