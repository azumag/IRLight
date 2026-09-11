from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from auth_store import AuthStateError, _validate_sessions  # noqa: E402


class AuthStateNumericValidationTest(unittest.TestCase):
    @staticmethod
    def _session_record(*, created_at: float, expires_at: float) -> dict[str, object]:
        return {
            "user_id": "user-a",
            "csrf_token": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "created_at": created_at,
            "expires_at": expires_at,
        }

    def test_huge_integer_timestamp_fails_with_controlled_state_error(self) -> None:
        state = {
            "sessions": {
                "0" * 64: {
                    "user_id": "user-a",
                    "csrf_token": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                    "created_at": 1.0,
                    "expires_at": 10**10_000,
                }
            }
        }

        with self.assertRaises(AuthStateError):
            _validate_sessions(state)

    def test_negative_session_created_at_is_rejected(self) -> None:
        state = {
            "sessions": {
                "0" * 64: self._session_record(created_at=-1.0, expires_at=1.0)
            }
        }

        with self.assertRaisesRegex(AuthStateError, "invalid created_at"):
            _validate_sessions(state)

    def test_session_expiry_before_creation_is_rejected(self) -> None:
        state = {
            "sessions": {
                "0" * 64: self._session_record(created_at=2.0, expires_at=1.0)
            }
        }

        with self.assertRaisesRegex(AuthStateError, "invalid expires_at"):
            _validate_sessions(state)

    def test_zero_length_session_timeline_remains_valid(self) -> None:
        state = {
            "sessions": {
                "0" * 64: self._session_record(created_at=1.0, expires_at=1.0)
            }
        }

        self.assertIs(_validate_sessions(state), state)


if __name__ == "__main__":
    unittest.main()
