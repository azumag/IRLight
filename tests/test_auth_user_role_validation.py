from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from auth_store import AuthStateError, _hash_password, _validate_users  # noqa: E402


_PASSWORD_HASH = _hash_password("correct-horse", salt=b"\x01" * 16)


def _users_state(*, role: str = "user") -> dict[str, object]:
    user_id = "user-a"
    email = "alice@example.com"
    return {
        "users": {
            user_id: {
                "id": user_id,
                "email": email,
                "password_hash": _PASSWORD_HASH,
                "display_name": "Alice",
                "role": role,
                "status": "active",
                "created_at": 100.0,
                "updated_at": 100.0,
            }
        },
        "email_index": {email: user_id},
    }


class AuthUserRoleValidationTest(unittest.TestCase):
    def test_canonical_roles_remain_valid_without_freezing_vocabulary(self) -> None:
        for role in ("user", "operator"):
            with self.subTest(role=role):
                state = _users_state(role=role)
                self.assertIs(_validate_users(state), state)

    def test_role_with_surrounding_whitespace_fails_closed(self) -> None:
        for role in (" user", "user ", "\tuser", "user\n", " "):
            with self.subTest(role=role):
                with self.assertRaisesRegex(AuthStateError, "invalid role"):
                    _validate_users(_users_state(role=role))


if __name__ == "__main__":
    unittest.main()
