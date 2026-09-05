from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from auth_store import AuthStateError, _validate_sessions  # noqa: E402


class AuthStateNumericValidationTest(unittest.TestCase):
    def test_huge_integer_timestamp_fails_with_controlled_state_error(self) -> None:
        state = {
            "sessions": {
                "token-hash": {
                    "user_id": "user-a",
                    "csrf_token": "csrf",
                    "created_at": 1.0,
                    "expires_at": 10**10_000,
                }
            }
        }

        with self.assertRaises(AuthStateError):
            _validate_sessions(state)


if __name__ == "__main__":
    unittest.main()
