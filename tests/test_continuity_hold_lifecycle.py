from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "provider"))
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from fake_provider import FakeProvider  # noqa: E402
from reaper import Reaper, ReaperConfig  # noqa: E402
from session_store import SessionStore  # noqa: E402


class ContinuityHoldLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SessionStore(self.tmp.name)
        self.provider = FakeProvider()
        self.config = ReaperConfig(provisioning_timeout_seconds=10, no_ingest_timeout_seconds=20, hold_timeout_seconds=30)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _holding_session(self, *, last_ingest_at: float = 100.0) -> str:
        session = self.store.create(user_id="user-1", environment="dev")
        session_id = str(session["session_id"])
        self.store.transition(session_id, "PROVISIONING")
        self.store.transition(session_id, "BOOTSTRAPPING")
        self.store.transition(session_id, "READY_WAIT_INGEST", ready_at=90.0)
        self.store.transition(session_id, "LIVE", first_ingest_at=95.0)
        self.store.transition(session_id, "HOLDING", last_ingest_at=last_ingest_at, hold_deadline_at=None)
        return session_id

    def test_missing_hold_deadline_is_recovered_from_last_ingest_time(self) -> None:
        session_id = self._holding_session()
        result = Reaper(self.store, self.provider, self.config, now=110.0).run()
        self.assertEqual(result["hold_deadlines_recovered"], 1)
        session = self.store.get(session_id)
        assert session is not None
        self.assertEqual(session["hold_deadline_at"], 130.0)
        self.assertEqual(session["events"][-1]["type"], "session.holding")

    def test_recovered_deadline_survives_store_restart_without_extending_window(self) -> None:
        session_id = self._holding_session()
        Reaper(self.store, self.provider, self.config, now=110.0).run()
        restarted = SessionStore(self.tmp.name)
        result = Reaper(restarted, self.provider, self.config, now=120.0).run()
        self.assertEqual(result["hold_deadlines_recovered"], 0)
        session = restarted.get(session_id)
        assert session is not None
        self.assertEqual(session["hold_deadline_at"], 130.0)

    def test_hold_timeout_finishes_session_with_auditable_reason(self) -> None:
        session_id = self._holding_session()
        Reaper(self.store, self.provider, self.config, now=110.0).run()
        result = Reaper(self.store, self.provider, self.config, now=131.0).run()
        self.assertEqual(result["deadline_stops"], 1)
        session = self.store.get(session_id)
        assert session is not None
        self.assertEqual(session["status"], "FINISHED")
        self.assertEqual([e["type"] for e in session["events"][-2:]], ["session.stopping", "session.finished"])
        self.assertTrue(all(e["reason_code"] == "HOLD_TIMEOUT" for e in session["events"][-2:]))

    def test_timeout_stop_wins_over_late_ingest_observation(self) -> None:
        session_id = self._holding_session()
        Reaper(self.store, self.provider, self.config, now=131.0).run()
        after = self.store.apply_ingest_observation(
            session_id,
            node_id="node-1",
            event_types=["ingest.reconnected"],
            observation={"status": "ACCEPTED", "path": "live/input", "online": True, "source_type": "rtmpConn", "source_id": "late-source", "bitrate_bps": 1500000, "tracks": [], "quality": None, "reasons": [], "warnings": [], "observed_at": 132.0},
            occurred_at=132.0,
        )
        self.assertEqual(after["status"], "FINISHED")
        self.assertFalse(any(e["type"] == "ingest.reconnected" for e in after["events"]))

    def test_ready_timeout_uses_distinct_reason_code(self) -> None:
        session = self.store.create(user_id="user-1", environment="dev")
        session_id = str(session["session_id"])
        self.store.transition(session_id, "PROVISIONING")
        self.store.transition(session_id, "BOOTSTRAPPING")
        self.store.transition(session_id, "READY_WAIT_INGEST", ready_at=0.0)
        Reaper(self.store, self.provider, self.config, now=21.0).run()
        finished = self.store.get(session_id)
        assert finished is not None
        self.assertEqual(finished["events"][-2]["reason_code"], "NO_INGEST_TIMEOUT")
        self.assertEqual(finished["events"][-1]["reason_code"], "NO_INGEST_TIMEOUT")


if __name__ == "__main__":
    unittest.main()
