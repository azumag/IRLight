from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from auth_store import AuthStateError, get_session_user  # noqa: E402


class AuthSessionRequestClockTest(unittest.TestCase):
    def test_invalid_system_clock_fails_before_authority_access(self) -> None:
        for bad_now in (
            float("nan"),
            float("inf"),
            float("-inf"),
            10**10_000,
        ):
            with self.subTest(bad_now=repr(bad_now)):
                with (
                    patch("auth_store.time.time", return_value=bad_now),
                    patch("auth_store._state_lock") as state_lock,
                ):
                    with self.assertRaisesRegex(
                        AuthStateError, "authentication clock is invalid"
                    ):
                        get_session_user("unused-token")
                state_lock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
