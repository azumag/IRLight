from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

import session_api  # noqa: E402
from session_store import SessionStore, new_session  # noqa: E402


class _EntitlementStore:
    def get(self, user_id: str) -> dict[str, object]:
        return {
            "id": f"user:{user_id}",
            "user_id": user_id,
            "max_concurrent_sessions": 1,
        }


class SessionCapacityCorruptionGuardTest(unittest.TestCase):
    def _record(self, session_id: str, *, status: str = "STOPPED") -> dict[str, object]:
        record = new_session(user_id="user-1", environment="dev")
        record["session_id"] = session_id
        record["status"] = status
        return record

    def _write_sessions(
        self,
        state_dir: str,
        sessions: dict[str, dict[str, object]],
    ) -> Path:
        path = Path(state_dir, "sessions.json")
        path.write_text(
            json.dumps(
                {
                    "sessions": sessions,
                    "orphan_cleanup_leases": {},
                }
            ),
            encoding="utf-8",
        )
        return path

    def _write_corrupt_capacity_record(
        self,
        state_dir: str,
        *,
        field: str,
        value: object,
    ) -> Path:
        record = self._record("existing-session", status="PROVISIONING")
        record[field] = value
        return self._write_sessions(state_dir, {"existing-session": record})

    def _assert_stable_unavailable(self, failure: HTTPException) -> None:
        self.assertEqual(failure.status_code, 503)
        self.assertEqual(
            failure.detail,
            {"code": session_api.SESSION_STATE_UNAVAILABLE_CODE},
        )

    def test_corrupt_capacity_record_blocks_new_prepare_before_provider_selection(self) -> None:
        cases = (
            ("status", "UNKNOWN_FUTURE_STATE"),
            ("entitlement_reserved", 0),
        )
        for field, value in cases:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as state_dir:
                path = self._write_corrupt_capacity_record(
                    state_dir,
                    field=field,
                    value=value,
                )
                before = path.read_bytes()

                with patch(
                    "session_api.default_store",
                    side_effect=lambda: SessionStore(state_dir),
                ), patch("session_api._validated_destination") as destination, patch(
                    "session_api.default_entitlement_store"
                ) as entitlement_factory, patch(
                    "session_api.default_provider"
                ) as provider_factory:
                    with self.assertRaises(HTTPException) as failure:
                        session_api.prepare_session(
                            "new-session",
                            session_api.PrepareRequest(environment="dev"),
                            {"id": "user-1"},
                            idempotency_key="capacity-corruption-guard",
                            _csrf=None,
                        )

                self._assert_stable_unavailable(failure.exception)
                self.assertEqual(path.read_bytes(), before)
                destination.assert_not_called()
                entitlement_factory.assert_not_called()
                provider_factory.assert_not_called()

    def test_corruption_after_replay_read_is_revalidated_before_provider_selection(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            path = self._write_sessions(
                state_dir,
                {
                    "existing-session": self._record("existing-session"),
                    "new-session": self._record("new-session"),
                },
            )
            store = SessionStore(state_dir)

            def corrupt_after_replay(
                _destination_id: str | None,
                _user_id: str,
                _egress_mode: str,
            ) -> None:
                payload = json.loads(path.read_text(encoding="utf-8"))
                payload["sessions"]["existing-session"]["status"] = (
                    "UNKNOWN_FUTURE_STATE"
                )
                path.write_text(json.dumps(payload), encoding="utf-8")
                return None

            with patch("session_api.default_store", return_value=store), patch(
                "session_api._validated_destination",
                side_effect=corrupt_after_replay,
            ), patch(
                "session_api.default_entitlement_store",
                return_value=_EntitlementStore(),
            ), patch("session_api.default_provider") as provider_factory:
                with self.assertRaises(HTTPException) as failure:
                    session_api.prepare_session(
                        "new-session",
                        session_api.PrepareRequest(environment="dev"),
                        {"id": "user-1"},
                        idempotency_key="capacity-corruption-race",
                        _csrf=None,
                    )

            self._assert_stable_unavailable(failure.exception)
            provider_factory.assert_not_called()
            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                persisted["sessions"]["existing-session"]["status"],
                "UNKNOWN_FUTURE_STATE",
            )
            self.assertEqual(
                persisted["sessions"]["new-session"]["status"],
                "STOPPED",
            )


if __name__ == "__main__":
    unittest.main()
