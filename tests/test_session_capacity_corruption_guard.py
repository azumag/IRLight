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


class SessionCapacityCorruptionGuardTest(unittest.TestCase):
    def _write_corrupt_capacity_record(
        self,
        state_dir: str,
        *,
        field: str,
        value: object,
    ) -> Path:
        record = new_session(user_id="user-1", environment="dev")
        record["session_id"] = "existing-session"
        record["status"] = "PROVISIONING"
        record[field] = value
        path = Path(state_dir, "sessions.json")
        path.write_text(
            json.dumps(
                {
                    "sessions": {"existing-session": record},
                    "orphan_cleanup_leases": {},
                }
            ),
            encoding="utf-8",
        )
        return path

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

                self.assertEqual(failure.exception.status_code, 503)
                self.assertEqual(
                    failure.exception.detail,
                    {"code": session_api.SESSION_STATE_UNAVAILABLE_CODE},
                )
                self.assertEqual(path.read_bytes(), before)
                destination.assert_not_called()
                entitlement_factory.assert_not_called()
                provider_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
