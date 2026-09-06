from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from session_store import (  # noqa: E402
    EntitlementExceeded,
    SessionStateError,
    SessionStore,
    new_session,
)


class SessionAuthorityValidationTest(unittest.TestCase):
    session_id = "session-1"

    def _record(self) -> dict[str, object]:
        record = new_session(user_id="user-1", environment="dev")
        record["session_id"] = self.session_id
        return record

    def _write_state(
        self,
        state_dir: str,
        record: dict[str, object],
        *,
        raw: str | None = None,
    ) -> Path:
        path = Path(state_dir, "sessions.json")
        if raw is None:
            raw = json.dumps(
                {
                    "sessions": {self.session_id: record},
                    "orphan_cleanup_leases": {},
                }
            )
        path.write_text(raw, encoding="utf-8")
        return path

    def test_non_finite_json_constants_fail_closed_without_rewrite(self) -> None:
        for constant in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(constant=constant), tempfile.TemporaryDirectory() as state_dir:
                # Build a simple valid payload explicitly; only updated_at is non-standard JSON.
                record = self._record()
                prefix = json.dumps(record, separators=(",", ":"))
                marker = f'"updated_at":{record["updated_at"]}'
                raw = (
                    '{"sessions":{"session-1":'
                    + prefix.replace(marker, f'"updated_at":{constant}', 1)
                    + '},"orphan_cleanup_leases":{}}'
                )
                path = self._write_state(state_dir, record, raw=raw)
                before = path.read_bytes()

                with self.assertRaises(SessionStateError):
                    SessionStore(state_dir)

                self.assertEqual(path.read_bytes(), before)

    def test_capacity_critical_identity_and_status_fields_fail_closed(self) -> None:
        cases = (
            ("session_id", "other-session"),
            ("user_id", ""),
            ("status", "UNKNOWN_FUTURE_STATE"),
            ("status", None),
            ("version", True),
            ("cleanup_pending", 0),
            ("entitlement_reserved", "false"),
            ("entitlement_reserved", 0),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as state_dir:
                record = self._record()
                record[field] = value
                self._write_state(state_dir, record)
                with self.assertRaises(SessionStateError):
                    SessionStore(state_dir)

    def test_session_timestamps_reject_type_confusion_and_huge_values(self) -> None:
        fields = (
            "created_at",
            "updated_at",
            "absolute_deadline_at",
            "hold_deadline_at",
            "node_last_heartbeat_at",
            "recovery_candidate_since",
        )
        for field in fields:
            for value in (True, "1234", 10**1000):
                with (
                    self.subTest(field=field, value=type(value).__name__),
                    tempfile.TemporaryDirectory() as state_dir,
                ):
                    record = self._record()
                    record[field] = value
                    self._write_state(state_dir, record)
                    with self.assertRaises(SessionStateError):
                        SessionStore(state_dir)

    def test_cleanup_lease_fields_fail_closed(self) -> None:
        cases = (
            {"lease_id": "lease-1", "scope": "bogus", "created_at": 1.0, "expires_at": 2.0},
            {"lease_id": "lease-1", "scope": "orphan", "resource_id": "server-1", "resource_kind": "server", "created_at": 1.0, "expires_at": float("inf")},
            {"lease_id": "lease-1", "scope": "orphan", "resource_id": "server-1", "resource_kind": "server", "created_at": 2.0, "expires_at": 1.0},
            {"lease_id": "lease-1", "scope": "session", "expected_states": ["UNKNOWN"], "created_at": 1.0, "expires_at": 2.0},
        )
        for lease in cases:
            with self.subTest(lease=lease), tempfile.TemporaryDirectory() as state_dir:
                path = Path(state_dir, "sessions.json")
                path.write_text(
                    json.dumps(
                        {
                            "sessions": {self.session_id: self._record()},
                            "orphan_cleanup_leases": {self.session_id: lease},
                        }
                    ),
                    encoding="utf-8",
                )
                with self.assertRaises(SessionStateError):
                    SessionStore(state_dir)

    def test_legacy_pre_entitlement_active_session_still_consumes_capacity(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            record = self._record()
            record.pop("entitlement_id")
            record.pop("entitlement_reserved")
            record["status"] = "PROVISIONING"
            self._write_state(state_dir, record)

            store = SessionStore(state_dir)
            with self.assertRaises(EntitlementExceeded):
                store.reserve_prepare_slot(
                    "session-2",
                    user_id="user-1",
                    environment="dev",
                    entitlement_id="user:user-1",
                    max_concurrent_sessions=1,
                )

    def test_writer_rejects_non_finite_timestamp_without_replacing_authority(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            store = SessionStore(state_dir)
            session = store.create(user_id="user-1", environment="dev")
            path = Path(state_dir, "sessions.json")
            before = path.read_bytes()

            with self.assertRaises(SessionStateError):
                store.update(str(session["session_id"]), absolute_deadline_at=float("inf"))

            self.assertEqual(path.read_bytes(), before)
            reloaded = SessionStore(state_dir).get(str(session["session_id"]))
            self.assertIsNotNone(reloaded)
            assert reloaded is not None
            self.assertIsNone(reloaded["absolute_deadline_at"])


if __name__ == "__main__":
    unittest.main()
