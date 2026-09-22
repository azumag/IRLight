from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from session_store import SessionStateError, SessionStore, new_session  # noqa: E402


class SessionPreEpochWriterGuardTest(unittest.TestCase):
    session_id = "session-clock-guard"

    def _record(self) -> dict[str, object]:
        with patch("session_store.time.time", return_value=1.0):
            record = new_session(user_id="user-1", environment="dev")
        record["session_id"] = self.session_id
        return record

    def _write_state(
        self,
        state_dir: str,
        record: dict[str, object],
        *,
        leases: dict[str, object] | None = None,
    ) -> Path:
        path = Path(state_dir, "sessions.json")
        path.write_text(
            json.dumps(
                {
                    "sessions": {self.session_id: record},
                    "orphan_cleanup_leases": leases or {},
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_persisted_negative_session_timestamps_fail_closed_without_rewrite(self) -> None:
        fields = (
            "created_at",
            "updated_at",
            "relay_client_updated_at",
            "node_registered_at",
            "node_last_heartbeat_at",
            "provisioning_started_at",
            "ready_at",
            "first_ingest_at",
            "last_ingest_at",
            "hold_deadline_at",
            "recovery_candidate_since",
            "absolute_deadline_at",
        )
        for field in fields:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as state_dir:
                record = self._record()
                record[field] = -1.0
                path = self._write_state(state_dir, record)
                before = path.read_bytes()

                with self.assertRaises(SessionStateError):
                    SessionStore(state_dir)

                self.assertEqual(path.read_bytes(), before)

    def test_persisted_negative_event_and_cleanup_lease_times_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            record = self._record()
            record["events"] = [
                {
                    "sequence": 1,
                    "type": "session.ready",
                    "reason_code": None,
                    "payload": {},
                    "occurred_at": -1.0,
                    "origin": "control-api",
                }
            ]
            record["next_event_seq"] = 2
            path = self._write_state(state_dir, record)
            before = path.read_bytes()

            with self.assertRaises(SessionStateError):
                SessionStore(state_dir)

            self.assertEqual(path.read_bytes(), before)

        with tempfile.TemporaryDirectory() as state_dir:
            record = self._record()
            leases = {
                self.session_id: {
                    "lease_id": "lease-1",
                    "scope": "orphan",
                    "resource_id": "server-1",
                    "resource_kind": "server",
                    "created_at": -1.0,
                    "expires_at": 2.0,
                }
            }
            path = self._write_state(state_dir, record, leases=leases)
            before = path.read_bytes()

            with self.assertRaises(SessionStateError):
                SessionStore(state_dir)

            self.assertEqual(path.read_bytes(), before)

    def test_default_writer_clock_fails_before_record_mutation(self) -> None:
        invalid_clocks: tuple[object, ...] = (
            -1.0,
            float("nan"),
            float("inf"),
            float("-inf"),
            10**1000,
        )
        for invalid_clock in invalid_clocks:
            with (
                self.subTest(clock=repr(invalid_clock)),
                tempfile.TemporaryDirectory() as state_dir,
            ):
                with patch("session_store.time.time", return_value=1.0):
                    store = SessionStore(state_dir)
                    session = store.create(user_id="user-1", environment="dev")
                session_id = str(session["session_id"])
                path = Path(state_dir, "sessions.json")
                before = path.read_bytes()

                with (
                    patch("session_store.time.time", return_value=invalid_clock),
                    self.assertRaises(SessionStateError),
                ):
                    store.update(session_id, failure_reason="must-not-stick")

                self.assertEqual(path.read_bytes(), before)
                self.assertIsNone(store._sessions[session_id]["failure_reason"])

    def test_explicit_negative_event_and_lifecycle_times_do_not_replace_authority(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            with patch("session_store.time.time", return_value=1.0):
                store = SessionStore(state_dir)
                session = store.create(user_id="user-1", environment="dev")
            session_id = str(session["session_id"])
            path = Path(state_dir, "sessions.json")
            before = path.read_bytes()

            with self.assertRaises(SessionStateError):
                store.append_event(
                    session_id,
                    event_type="session.ready",
                    payload={},
                    occurred_at=-1.0,
                )
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(store._sessions[session_id]["events"], [])

            with self.assertRaises(SessionStateError):
                store.update(session_id, hold_deadline_at=-1.0)
            self.assertEqual(path.read_bytes(), before)
            self.assertIsNone(store._sessions[session_id]["hold_deadline_at"])

            with self.assertRaises(SessionStateError):
                store.claim_provisioning(
                    session_id,
                    operation_id="op-1",
                    started_at=-1.0,
                )
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(store._sessions[session_id]["status"], "STOPPED")

    def test_absolute_deadline_overflow_fails_before_authority_creation(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            store = SessionStore(state_dir)
            path = Path(state_dir, "sessions.json")

            with (
                patch("session_store.time.time", return_value=1.0),
                self.assertRaises(SessionStateError),
            ):
                store.create(
                    user_id="user-1",
                    environment="dev",
                    absolute_deadline_hours=10**1000,
                )

            self.assertFalse(path.exists())
            self.assertEqual(store._sessions, {})

    def test_cleanup_lease_expiry_overflow_does_not_replace_authority(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            with patch("session_store.time.time", return_value=1.0):
                store = SessionStore(state_dir)
                session = store.create(user_id="user-1", environment="dev")
            session_id = str(session["session_id"])
            path = Path(state_dir, "sessions.json")
            before = path.read_bytes()

            with (
                patch("session_store.time.time", return_value=1.0),
                self.assertRaises(SessionStateError),
            ):
                store.claim_orphan_cleanup(
                    session_id,
                    resource_id="server-1",
                    resource_kind="server",
                    lease_seconds=10**1000,
                )

            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(store._orphan_cleanup_leases, {})

    def test_unix_epoch_zero_remains_valid(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            with patch("session_store.time.time", return_value=0.0):
                store = SessionStore(state_dir)
                session = store.create(user_id="user-1", environment="dev")
                session_id = str(session["session_id"])
                event = store.append_event(
                    session_id,
                    event_type="session.ready",
                    payload={},
                    occurred_at=0.0,
                )
                lease_id = store.claim_orphan_cleanup(
                    session_id,
                    resource_id="server-1",
                    resource_kind="server",
                    lease_seconds=1.0,
                )

            self.assertEqual(session["created_at"], 0.0)
            self.assertEqual(session["updated_at"], 0.0)
            self.assertEqual(event["occurred_at"], 0.0)
            self.assertIsNotNone(lease_id)
            reloaded = SessionStore(state_dir)
            persisted = reloaded.get(session_id)
            assert persisted is not None
            self.assertEqual(persisted["created_at"], 0.0)
            self.assertEqual(persisted["events"][0]["occurred_at"], 0.0)


if __name__ == "__main__":
    unittest.main()
