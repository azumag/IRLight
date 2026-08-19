from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "provider"))
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from session_store import SESSION_EVENT_LIMIT, SessionStore  # noqa: E402


class SessionEventsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = SessionStore(tempfile.mkdtemp(prefix="irlight-sessions-"))
        self.session = self.store.create(user_id="deadbeef", environment="dev")
        self.session_id = str(self.session["session_id"])

    def _add(self, event_type: str, reason_code: str | None = None, payload: dict | None = None):
        return self.store.append_event(
            self.session_id,
            event_type=event_type,
            reason_code=reason_code,
            payload=payload or {},
            origin="test",
            occurred_at=1.0,
        )

    def test_events_are_appended_sequentially(self) -> None:
        first = self._add("LIVE")
        second = self._add("HOLDING", reason_code="input_lost")
        session = self.store.get(self.session_id)
        assert session is not None
        events = session["events"]
        self.assertEqual([e["sequence"] for e in events], [1, 2])
        self.assertEqual(events[1]["reason_code"], "input_lost")
        self.assertEqual(first["origin"], "test")
        self.assertEqual(second["sequence"], 2)

    def test_events_are_persisted(self) -> None:
        self._add("INPUT_LOST", payload={"duration_seconds": 12})
        reloaded = SessionStore(self.store.state_dir)
        session = reloaded.get(self.session_id)
        assert session is not None
        self.assertEqual(len(session["events"]), 1)
        self.assertEqual(session["events"][0]["payload"], {"duration_seconds": 12})
        self.assertEqual(session["next_event_seq"], 2)

    def test_event_stream_is_bounded_without_reusing_sequence_numbers(self) -> None:
        for index in range(SESSION_EVENT_LIMIT + 5):
            self.store.append_event(
                self.session_id,
                event_type="sample",
                payload={"index": index},
                occurred_at=float(index),
            )
        session = self.store.get(self.session_id)
        assert session is not None
        events = session["events"]
        self.assertEqual(len(events), SESSION_EVENT_LIMIT)
        self.assertEqual(events[0]["sequence"], 6)
        self.assertEqual(events[-1]["sequence"], SESSION_EVENT_LIMIT + 5)
        self.assertEqual(session["next_event_seq"], SESSION_EVENT_LIMIT + 6)


if __name__ == "__main__":
    unittest.main()
