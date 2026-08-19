from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

import node_internal  # noqa: E402


class _SessionStore:
    def __init__(self) -> None:
        self.session = {
            "session_id": "session-1",
            "node_id": "node-0001",
            "status": "LIVE",
            "events": [],
        }
        self.events: list[dict[str, object]] = []

    def get(self, session_id: str):
        return dict(self.session) if session_id == "session-1" else None

    def append_event(self, session_id: str, **kwargs):
        self.events.append(dict(kwargs))
        return kwargs

    def update(self, session_id: str, **changes):
        self.session.update(changes)
        return dict(self.session)


class EgressEventTest(unittest.TestCase):
    def observation(self, status: str, connected: bool, reason: str | None = None):
        return {
            "status": status,
            "connected": connected,
            "attempt": 1,
            "reason_code": reason,
            "rendered_buffers": 3 if connected else 0,
            "next_retry_at": None,
            "destination_scheme": "rtmps",
            "destination_host": "live.example",
            "observed_at": 100.0,
        }

    def test_first_connect_then_reconnect_emits_expected_sequence(self) -> None:
        node = {
            "node_id": "node-0001",
            "events": [],
            "next_event_seq": 1,
            "egress_ever_connected": False,
        }
        starting = self.observation("STARTING", False)
        connected = self.observation("CONNECTED", True)
        reconnecting = self.observation("RECONNECTING", False, "UNREACHABLE")

        self.assertEqual(
            node_internal._append_egress_events(node, None, starting),
            ["egress.starting"],
        )
        self.assertEqual(
            node_internal._append_egress_events(node, starting, connected),
            ["egress.connected"],
        )
        self.assertEqual(
            node_internal._append_egress_events(node, connected, reconnecting),
            ["egress.disconnected", "egress.reconnecting"],
        )
        self.assertEqual(
            node_internal._append_egress_events(node, reconnecting, connected),
            ["egress.recovered"],
        )

    def test_auth_failed_is_not_treated_as_reconnect(self) -> None:
        previous = self.observation("CONNECTED", True)
        current = self.observation("AUTH_FAILED", False, "AUTH_FAILED")
        self.assertEqual(
            node_internal._egress_event_types(
                previous, current, had_connection=True
            ),
            ["egress.disconnected", "egress.auth_failed"],
        )

    def test_session_events_and_state_are_secret_safe(self) -> None:
        store = _SessionStore()
        current = self.observation("RECONNECTING", False, "UNREACHABLE")
        current["ignored_secret"] = "super-secret-stream-key"
        with patch("node_internal.default_store", return_value=store):
            node_internal._apply_egress_to_session(
                session_id="session-1",
                node_id="node-0001",
                event_types=["egress.disconnected", "egress.reconnecting"],
                current=current,
            )
        self.assertEqual(store.session["egress_status"], "RECONNECTING")
        self.assertFalse(store.session["egress_connected"])
        self.assertEqual(store.session["egress_last_reason"], "UNREACHABLE")
        self.assertEqual(len(store.events), 2)
        dump = str(store.events)
        self.assertNotIn("ignored_secret", dump)
        self.assertNotIn("super-secret-stream-key", dump)
        self.assertIn("live.example", dump)


if __name__ == "__main__":
    unittest.main()
