from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "provider"))
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from session_store import (  # noqa: E402
    SESSION_EVENT_LIMIT,
    SESSION_USER_EVENT_LIMIT,
    USER_EVENT_ORIGIN,
    SessionStore,
)


class SessionUserEventRetentionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = SessionStore(tempfile.mkdtemp(prefix="irlight-sessions-"))
        self.session = self.store.create(user_id="deadbeef", environment="dev")
        self.session_id = str(self.session["session_id"])

    def _add(self, event_type: str, *, origin: str = "test", occurred_at: float = 1.0):
        return self.store.append_event(
            self.session_id,
            event_type=event_type,
            payload={},
            origin=origin,
            occurred_at=occurred_at,
        )

    def _retained(self) -> list[dict]:
        session = self.store.get(self.session_id)
        assert session is not None
        return list(session["events"])

    def test_user_event_flood_cannot_evict_internal_audit_events(self) -> None:
        """Regression for #93: mass user posts must not push audit events out."""
        self._add("session.failed", origin="node-agent")
        for index in range(SESSION_USER_EVENT_LIMIT + 50):
            self._add("user.note", origin=USER_EVENT_ORIGIN, occurred_at=2.0 + index)

        events = self._retained()
        internal = [e for e in events if e["origin"] != USER_EVENT_ORIGIN]
        user = [e for e in events if e["origin"] == USER_EVENT_ORIGIN]

        self.assertEqual(len(internal), 1)
        self.assertEqual(internal[0]["type"], "session.failed")
        self.assertEqual(internal[0]["sequence"], 1)
        self.assertEqual(len(user), SESSION_USER_EVENT_LIMIT)

    def test_user_ring_keeps_newest_events_and_merged_order(self) -> None:
        for index in range(SESSION_USER_EVENT_LIMIT + 10):
            self._add("user.note", origin=USER_EVENT_ORIGIN, occurred_at=float(index))

        events = self._retained()
        sequences = [int(e["sequence"]) for e in events]
        self.assertEqual(len(events), SESSION_USER_EVENT_LIMIT)
        self.assertEqual(sequences, sorted(sequences))
        self.assertEqual(sequences[0], 11)
        self.assertEqual(sequences[-1], SESSION_USER_EVENT_LIMIT + 10)

    def test_internal_ring_limit_is_unchanged_when_user_events_exist(self) -> None:
        for index in range(SESSION_EVENT_LIMIT):
            self._add("session.sample", origin="control-api", occurred_at=float(index))
        for index in range(SESSION_USER_EVENT_LIMIT + 5):
            self._add(
                "user.note",
                origin=USER_EVENT_ORIGIN,
                occurred_at=float(SESSION_EVENT_LIMIT + index),
            )
        for index in range(5):
            self._add(
                "session.sample",
                origin="control-api",
                occurred_at=float(SESSION_EVENT_LIMIT + SESSION_USER_EVENT_LIMIT + index),
            )

        events = self._retained()
        internal = [e for e in events if e["origin"] != USER_EVENT_ORIGIN]
        user = [e for e in events if e["origin"] == USER_EVENT_ORIGIN]

        self.assertEqual(len(internal), SESSION_EVENT_LIMIT)
        self.assertEqual(len(user), SESSION_USER_EVENT_LIMIT)
        self.assertEqual(int(internal[0]["sequence"]), 6)
        self.assertEqual(int(internal[-1]["sequence"]), SESSION_EVENT_LIMIT + SESSION_USER_EVENT_LIMIT + 10)
        self.assertLessEqual(len(events), SESSION_EVENT_LIMIT + SESSION_USER_EVENT_LIMIT)

    def test_user_eviction_may_leave_sequence_gaps_but_pagination_still_works(self) -> None:
        """User-ring eviction can leave mid-list sequence gaps; a cursor must
        still return every retained event above it."""
        self._add("session.boundary", origin="control-api")
        for index in range(SESSION_USER_EVENT_LIMIT + 20):
            self._add("user.note", origin=USER_EVENT_ORIGIN, occurred_at=10.0 + index)
        tail = self._add("session.tail", origin="control-api", occurred_at=999.0)

        events = self._retained()
        sequences = [int(e["sequence"]) for e in events]
        # The dropped user events leave a gap between the first internal event
        # and the oldest retained user note; both internal events remain.
        self.assertIn(1, sequences)
        self.assertIn(int(tail["sequence"]), sequences)
        self.assertEqual(sequences, sorted(sequences))
        user_sequences = [
            s for s, e in zip(sequences, events) if e["origin"] == USER_EVENT_ORIGIN
        ]
        self.assertEqual(len(user_sequences), SESSION_USER_EVENT_LIMIT)
        self.assertGreater(user_sequences[0], 1)

    def test_just_appended_user_event_is_always_retained(self) -> None:
        for index in range(SESSION_USER_EVENT_LIMIT + 5):
            self._add("user.note", origin=USER_EVENT_ORIGIN, occurred_at=float(index))
        latest = self._add("user.note", origin=USER_EVENT_ORIGIN, occurred_at=9999.0)

        events = self._retained()
        self.assertEqual(int(events[-1]["sequence"]), int(latest["sequence"]))

    def test_dual_ring_survives_persistence_reload(self) -> None:
        self._add("session.audit", origin="node-agent")
        for index in range(SESSION_USER_EVENT_LIMIT + 30):
            self._add("user.note", origin=USER_EVENT_ORIGIN, occurred_at=float(index))

        reloaded = SessionStore(self.store.state_dir)
        session = reloaded.get(self.session_id)
        assert session is not None
        internal = [e for e in session["events"] if e["origin"] != USER_EVENT_ORIGIN]
        user = [e for e in session["events"] if e["origin"] == USER_EVENT_ORIGIN]
        self.assertEqual(len(internal), 1)
        self.assertEqual(len(user), SESSION_USER_EVENT_LIMIT)

    def test_legacy_state_with_excess_user_events_is_bounded_on_next_append(self) -> None:
        """States persisted before the user ring existed are not rejected; the
        bound applies on the next append without touching internal events."""
        seeded = [
            {
                "sequence": index + 1,
                "type": "user.note",
                "reason_code": None,
                "payload": {},
                "occurred_at": float(index),
                "origin": USER_EVENT_ORIGIN,
            }
            for index in range(SESSION_USER_EVENT_LIMIT + 40)
        ]
        seeded.append(
            {
                "sequence": len(seeded) + 1,
                "type": "session.failed",
                "reason_code": None,
                "payload": {},
                "occurred_at": float(len(seeded)),
                "origin": "node-agent",
            }
        )
        self.store.update(
            self.session_id,
            events=seeded,
            next_event_seq=len(seeded) + 1,
        )

        self._add("user.note", origin=USER_EVENT_ORIGIN, occurred_at=9999.0)

        events = self._retained()
        internal = [e for e in events if e["origin"] != USER_EVENT_ORIGIN]
        user = [e for e in events if e["origin"] == USER_EVENT_ORIGIN]
        self.assertEqual(len(internal), 1)
        self.assertEqual(internal[0]["type"], "session.failed")
        self.assertEqual(len(user), SESSION_USER_EVENT_LIMIT)


if __name__ == "__main__":
    unittest.main()
