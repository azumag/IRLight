from __future__ import annotations

import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "provider"))
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from session_store import SessionStore  # noqa: E402


class SessionEventsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = SessionStore(tempfile.mkdtemp(prefix="irlight-sessions-"))
        self.session = self.store.create(user_id="deadbeef", environment="dev")
        self.session_id = str(self.session["session_id"])

    def _add(self, event_type: str, reason_code: str | None = None, payload: dict | None = None):
        session = self.store.get(self.session_id)
        events = list(session.get("events", []))
        event = {
            "sequence": len(events) + 1,
            "type": event_type,
            "reason_code": reason_code,
            "payload": payload or {},
            "occurred_at": 1,
        }
        events.append(event)
        self.store.update(self.session_id, events=events)
        return event

    def test_events_are_appended_sequentially(self) -> None:
        self._add("LIVE")
        self._add("HOLDING", reason_code="input_lost")
        session = self.store.get(self.session_id)
        events = session["events"]
        self.assertEqual([e["sequence"] for e in events], [1, 2])
        self.assertEqual(events[1]["reason_code"], "input_lost")

    def test_events_are_persisted(self) -> None:
        self._add("INPUT_LOST", payload={"duration_seconds": 12})
        reloaded = SessionStore(self.store.state_dir)
        session = reloaded.get(self.session_id)
        self.assertEqual(len(session["events"]), 1)
        self.assertEqual(session["events"][0]["payload"], {"duration_seconds": 12})


if __name__ == "__main__":
    unittest.main()