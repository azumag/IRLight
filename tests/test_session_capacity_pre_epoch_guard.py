from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTROL_API = ROOT / "apps" / "control-api"
if str(CONTROL_API) not in sys.path:
    sys.path.insert(0, str(CONTROL_API))

from session_capacity_inspect_cli import (  # noqa: E402
    SessionCapacityInspectError,
    _read_sessions,
    main,
)
from session_store import new_session  # noqa: E402


class SessionCapacityPreEpochGuardTest(unittest.TestCase):
    session_id = "session-pre-epoch"

    def _record(self) -> dict[str, object]:
        record = new_session(user_id="user-a", environment="dev")
        record["session_id"] = self.session_id
        return record

    def _write_state(
        self,
        state_dir: Path,
        record: dict[str, object],
        *,
        leases: dict[str, dict[str, object]] | None = None,
    ) -> Path:
        path = state_dir / "sessions.json"
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

    def test_pre_epoch_session_timestamps_fail_closed_without_rewrite(self) -> None:
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
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                state_dir = Path(directory)
                record = self._record()
                record[field] = -1.0
                path = self._write_state(state_dir, record)
                before = path.read_bytes()

                with self.assertRaises(SessionCapacityInspectError):
                    _read_sessions(state_dir)

                self.assertEqual(path.read_bytes(), before)

    def test_pre_epoch_event_time_fails_closed_without_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
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

            with self.assertRaises(SessionCapacityInspectError):
                _read_sessions(state_dir)

            self.assertEqual(path.read_bytes(), before)

    def test_pre_epoch_cleanup_lease_fails_closed_without_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            record = self._record()
            path = self._write_state(
                state_dir,
                record,
                leases={
                    self.session_id: {
                        "lease_id": "lease-1",
                        "scope": "orphan",
                        "resource_id": "server-1",
                        "resource_kind": "server",
                        "created_at": -2.0,
                        "expires_at": -1.0,
                    }
                },
            )
            before = path.read_bytes()

            with self.assertRaises(SessionCapacityInspectError):
                _read_sessions(state_dir)

            self.assertEqual(path.read_bytes(), before)

    def test_epoch_zero_remains_valid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            record = self._record()
            record["created_at"] = 0.0
            record["updated_at"] = 0.0
            record["events"] = [
                {
                    "sequence": 1,
                    "type": "session.ready",
                    "reason_code": None,
                    "payload": {},
                    "occurred_at": 0.0,
                    "origin": "control-api",
                }
            ]
            record["next_event_seq"] = 2
            self._write_state(
                state_dir,
                record,
                leases={
                    self.session_id: {
                        "lease_id": "lease-1",
                        "scope": "orphan",
                        "resource_id": "server-1",
                        "resource_kind": "server",
                        "created_at": 0.0,
                        "expires_at": 1.0,
                    }
                },
            )

            sessions = _read_sessions(state_dir)

            self.assertEqual(set(sessions), {self.session_id})

    def test_cli_reports_pre_epoch_session_authority_as_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            record = self._record()
            record["created_at"] = -1.0
            path = self._write_state(state_dir, record)
            before = path.read_bytes()
            output = io.StringIO()

            with redirect_stdout(output):
                exit_code = main(
                    [
                        "--state-dir",
                        str(state_dir),
                        "--user-id",
                        "sensitive-user-id",
                    ]
                )

            self.assertEqual(path.read_bytes(), before)

        self.assertEqual(exit_code, 3)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "CAPACITY_UNAVAILABLE")
        self.assertEqual(payload["authority"], "sessions")
        self.assertNotIn("sensitive-user-id", output.getvalue())
        self.assertNotIn(str(state_dir), output.getvalue())


if __name__ == "__main__":
    unittest.main()
