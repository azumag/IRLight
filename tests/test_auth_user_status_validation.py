from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from auth_store import AuthStateError, _hash_password, _validate_users  # noqa: E402


_PASSWORD_HASH = _hash_password("correct-horse", salt=b"\x01" * 16)


def _users_state(*, status: str = "active") -> dict[str, object]:
    user_id = "user-a"
    email = "alice@example.com"
    return {
        "users": {
            user_id: {
                "id": user_id,
                "email": email,
                "password_hash": _PASSWORD_HASH,
                "display_name": "Alice",
                "role": "user",
                "status": status,
                "created_at": 100.0,
                "updated_at": 100.0,
            }
        },
        "email_index": {email: user_id},
    }


class AuthUserStatusValidationTest(unittest.TestCase):
    def test_active_status_remains_valid(self) -> None:
        state = _users_state()
        self.assertIs(_validate_users(state), state)

    def test_unknown_statuses_fail_closed(self) -> None:
        for status in ("disabled", "ACTIVE", " active ", "suspended"):
            with self.subTest(status=status):
                with self.assertRaisesRegex(AuthStateError, "invalid status"):
                    _validate_users(_users_state(status=status))


if __name__ == "__main__":
    unittest.main()
