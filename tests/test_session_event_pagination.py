from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "provider"))
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

import session_api  # noqa: E402


def _event(sequence: int) -> dict[str, object]:
    return {
        "sequence": sequence,
        "type": "sample",
        "reason_code": None,
        "payload": {"sequence": sequence},
        "occurred_at": float(sequence),
        "origin": "test",
    }


class SessionEventPaginationTest(unittest.TestCase):
    def test_default_listing_keeps_all_retained_events_and_adds_cursor_metadata(self) -> None:
        session = {"session_id": "session-1", "user_id": "user-1", "events": [_event(4), _event(5)]}
        with patch("session_api._owned_session", return_value=session):
            result = session_api.list_session_events("session-1", {"id": "user-1"})

        self.assertEqual([event["sequence"] for event in result["events"]], [4, 5])
        self.assertEqual(result["earliest_sequence"], 4)
        self.assertEqual(result["latest_sequence"], 5)
        self.assertEqual(result["next_after_sequence"], 5)
        self.assertFalse(result["has_more"])
        self.assertFalse(result["retention_gap"])

    def test_after_sequence_and_limit_form_a_monotonic_cursor(self) -> None:
        session = {
            "session_id": "session-1",
            "user_id": "user-1",
            "events": [_event(10), _event(11), _event(12), _event(13)],
        }
        with patch("session_api._owned_session", return_value=session):
            first = session_api.list_session_events(
                "session-1", {"id": "user-1"}, after_sequence=10, limit=2
            )
            second = session_api.list_session_events(
                "session-1",
                {"id": "user-1"},
                after_sequence=first["next_after_sequence"],
                limit=2,
            )

        self.assertEqual([event["sequence"] for event in first["events"]], [11, 12])
        self.assertTrue(first["has_more"])
        self.assertEqual(first["next_after_sequence"], 12)
        self.assertEqual([event["sequence"] for event in second["events"]], [13])
        self.assertFalse(second["has_more"])
        self.assertEqual(second["next_after_sequence"], 13)

    def test_retention_gap_is_explicit_when_requested_cursor_predates_retained_ring(self) -> None:
        session = {
            "session_id": "session-1",
            "user_id": "user-1",
            "events": [_event(6), _event(7), _event(8)],
        }
        with patch("session_api._owned_session", return_value=session):
            result = session_api.list_session_events(
                "session-1", {"id": "user-1"}, after_sequence=2, limit=2
            )

        self.assertTrue(result["retention_gap"])
        self.assertEqual(result["earliest_sequence"], 6)
        self.assertEqual([event["sequence"] for event in result["events"]], [6, 7])
        self.assertTrue(result["has_more"])

    def test_cursor_on_retained_boundary_does_not_report_gap(self) -> None:
        session = {
            "session_id": "session-1",
            "user_id": "user-1",
            "events": [_event(6), _event(7)],
        }
        with patch("session_api._owned_session", return_value=session):
            result = session_api.list_session_events(
                "session-1", {"id": "user-1"}, after_sequence=5, limit=10
            )

        self.assertFalse(result["retention_gap"])
        self.assertEqual([event["sequence"] for event in result["events"]], [6, 7])

    def test_invalid_cursor_and_limit_are_rejected(self) -> None:
        session = {"session_id": "session-1", "user_id": "user-1", "events": []}
        with patch("session_api._owned_session", return_value=session):
            with self.assertRaises(session_api.HTTPException) as cursor_error:
                session_api.list_session_events(
                    "session-1", {"id": "user-1"}, after_sequence=-1
                )
            with self.assertRaises(session_api.HTTPException) as limit_error:
                session_api.list_session_events(
                    "session-1",
                    {"id": "user-1"},
                    limit=session_api.SESSION_EVENT_LIMIT + 1,
                )

        self.assertEqual(cursor_error.exception.status_code, 400)
        self.assertEqual(limit_error.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
