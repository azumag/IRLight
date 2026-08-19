from __future__ import annotations

import os
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
        self.old_stable = os.environ.get("SESSION_RECOVERY_STABLE_SECONDS")
        os.environ["SESSION_RECOVERY_STABLE_SECONDS"] = "3"
        self.store = SessionStore(self.tmp.name)

    def tearDown(self) -> None:
        if self.old_stable is None:
            os.environ.pop("SESSION_RECOVERY_STABLE_SECONDS", None)
        else:
            os.environ["SESSION_RECOVERY_STABLE_SECONDS"] = self.old_stable
        self.tmp.cleanup()

    def _holding_session(self) -> str:
        session = self.store.create(user_id="user-1", environment="dev")
        session_id = str(session["session_id"])
        self.store.transition(session_id, "PROVISIONING")
        self.store.transition(session_id, "BOOTSTRAPPING")
        self.store.transition(session_id, "READY_WAIT_INGEST", ready_at=80.0)
        self.store.transition(session_id, "LIVE", first_ingest_at=85.0)
        self.store.transition(
            session_id,
            "HOLDING",
            last_ingest_at=90.0,
            hold_deadline_at=120.0,
        )
        return session_id

    @staticmethod
    def _observation(
        *,
        status: str = "ACCEPTED",
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
            "tracks": [],
            "quality": {"video_fps": 30.0} if online else None,
            "reasons": reasons or [],
            "warnings": [],
            "enforced": False,
            "observed_at": 0.0,
        }

    def _apply(
        self,
        session_id: str,
        at: float,
        observation: dict[str, object],
    ) -> dict[str, object]:
        return self.store.apply_ingest_observation(
            session_id,
            node_id="node-1",
            event_types=[],
            observation=observation,
            occurred_at=at,
        )

    def test_holding_requires_full_stability_window_before_live(self) -> None:
        session_id = self._holding_session()

        first = self._apply(session_id, 100.0, self._observation())
        self.assertEqual(first["status"], "HOLDING")
        self.assertEqual(first["recovery_candidate_started_at"], 100.0)
        self.assertEqual(first["recovery_candidate_target"], "LIVE")
        self.assertEqual(first["last_ingest_at"], 90.0)
        self.assertEqual(first["hold_deadline_at"], 120.0)

        second = self._apply(session_id, 102.9, self._observation())
        self.assertEqual(second["status"], "HOLDING")
        self.assertEqual(second["last_ingest_at"], 90.0)

        recovered = self._apply(session_id, 103.0, self._observation())
        self.assertEqual(recovered["status"], "LIVE")
        self.assertIsNone(recovered["recovery_candidate_started_at"])
        self.assertIsNone(recovered["hold_deadline_at"])
        self.assertEqual(recovered["last_ingest_at"], 103.0)
        event = recovered["events"][-1]
        self.assertEqual(event["type"], "session.recovered")
        self.assertEqual(event["payload"]["from_state"], "HOLDING")
        self.assertEqual(event["payload"]["to_state"], "LIVE")
        self.assertGreaterEqual(event["payload"]["recovery_stable_seconds"], 3.0)

    def test_source_change_resets_stability_window(self) -> None:
        session_id = self._holding_session()
        self._apply(session_id, 100.0, self._observation(source_id="source-a"))
        changed = self._apply(
            session_id, 102.0, self._observation(source_id="source-b")
        )
        self.assertEqual(changed["status"], "HOLDING")
        self.assertEqual(changed["recovery_candidate_started_at"], 102.0)
        self.assertEqual(changed["recovery_candidate_source_id"], "source-b")

        almost = self._apply(
            session_id, 104.9, self._observation(source_id="source-b")
        )
        self.assertEqual(almost["status"], "HOLDING")
        recovered = self._apply(
            session_id, 105.0, self._observation(source_id="source-b")
        )
        self.assertEqual(recovered["status"], "LIVE")

    def test_quality_target_change_resets_stability_window(self) -> None:
        session_id = self._holding_session()
        self._apply(
            session_id,
            100.0,
            self._observation(
                status="DEGRADED", reasons=["FPS_OUT_OF_RANGE"]
            ),
        )
        changed = self._apply(session_id, 102.0, self._observation(status="ACCEPTED"))
        self.assertEqual(changed["status"], "HOLDING")
        self.assertEqual(changed["recovery_candidate_started_at"], 102.0)
        self.assertEqual(changed["recovery_candidate_target"], "LIVE")

        recovered = self._apply(session_id, 105.0, self._observation(status="ACCEPTED"))
        self.assertEqual(recovered["status"], "LIVE")

    def test_offline_flap_clears_candidate_and_does_not_extend_hold(self) -> None:
        session_id = self._holding_session()
        self._apply(session_id, 100.0, self._observation())
        offline = self._apply(
            session_id,
            101.0,
            self._observation(status="OFFLINE", online=False, source_id=None),
        )
        self.assertEqual(offline["status"], "HOLDING")
        self.assertIsNone(offline["recovery_candidate_started_at"])
        self.assertEqual(offline["last_ingest_at"], 90.0)
        self.assertEqual(offline["hold_deadline_at"], 120.0)

        restarted = self._apply(session_id, 102.0, self._observation())
        self.assertEqual(restarted["recovery_candidate_started_at"], 102.0)
        self.assertEqual(restarted["status"], "HOLDING")
        recovered = self._apply(session_id, 105.0, self._observation())
        self.assertEqual(recovered["status"], "LIVE")

    def test_candidate_survives_control_plane_store_restart(self) -> None:
        session_id = self._holding_session()
        self._apply(session_id, 100.0, self._observation())

        restarted_store = SessionStore(self.tmp.name)
        self.store = restarted_store
        candidate = self.store.get(session_id)
        assert candidate is not None
        self.assertEqual(candidate["status"], "HOLDING")
        self.assertEqual(candidate["recovery_candidate_started_at"], 100.0)

        recovered = self._apply(session_id, 103.1, self._observation())
        self.assertEqual(recovered["status"], "LIVE")

    def test_stable_degraded_recovery_promotes_to_degraded_with_reason(self) -> None:
        session_id = self._holding_session()
        degraded = self._observation(
            status="DEGRADED", reasons=["BITRATE_TOO_LOW"]
        )
        first = self._apply(session_id, 100.0, degraded)
        self.assertEqual(first["status"], "HOLDING")
        self.assertEqual(first["recovery_candidate_target"], "DEGRADED")

        recovered = self._apply(session_id, 103.0, degraded)
        self.assertEqual(recovered["status"], "DEGRADED")
        event = recovered["events"][-1]
        self.assertEqual(event["type"], "session.recovered")
        self.assertEqual(event["reason_code"], "BITRATE_TOO_LOW")
        self.assertEqual(event["payload"]["to_state"], "DEGRADED")

    def test_zero_stability_window_allows_immediate_recovery(self) -> None:
        session_id = self._holding_session()
        os.environ["SESSION_RECOVERY_STABLE_SECONDS"] = "0"
        recovered = self._apply(session_id, 100.0, self._observation())
        self.assertEqual(recovered["status"], "LIVE")


if __name__ == "__main__":
    unittest.main()
