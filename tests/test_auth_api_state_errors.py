from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException, Response


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

import auth_api  # noqa: E402
from auth_store import AuthError, AuthStateError, InvalidCredentials  # noqa: E402


class AuthApiStateErrorTest(unittest.TestCase):
    def assert_state_unavailable(self, callback) -> None:
        with self.assertRaises(HTTPException) as raised:
            callback()
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(
            raised.exception.detail,
            {"code": auth_api.AUTH_STATE_UNAVAILABLE_CODE},
        )
        self.assertNotIn("/state/", str(raised.exception.detail))

    def test_require_user_maps_state_error_to_stable_503(self) -> None:
        with patch(
            "auth_api.get_session_user",
            side_effect=AuthStateError("authentication state /state/users.json cannot be read"),
        ):
            self.assert_state_unavailable(
                lambda: auth_api.require_user(session_token="session-token")
            )

    def test_require_csrf_maps_state_error_to_stable_503(self) -> None:
        with patch(
            "auth_api.get_session_user",
            side_effect=AuthStateError(
                "authentication state /state/auth_sessions.json cannot be read"
            ),
        ):
            self.assert_state_unavailable(
                lambda: auth_api.require_csrf(
                    session_token="session-token",
                    csrf_cookie="csrf",
                    x_csrf_token="csrf",
                )
            )

    def test_register_maps_state_error_before_generic_auth_error(self) -> None:
        request = auth_api.RegisterRequest(
            email="alice@example.com",
            password="correct-horse",
        )
        with patch(
            "auth_api.register_user",
            side_effect=AuthStateError("user state /state/users.json is corrupt"),
        ):
            self.assert_state_unavailable(lambda: auth_api.register(request))

    def test_login_maps_authenticate_state_error_to_stable_503(self) -> None:
        request = auth_api.LoginRequest(
            email="alice@example.com",
            password="correct-horse",
        )
        response = Response()
        with patch(
            "auth_api.authenticate_user",
            side_effect=AuthStateError("user state /state/users.json is corrupt"),
        ):
            self.assert_state_unavailable(lambda: auth_api.login(request, response))

    def test_login_maps_session_write_state_error_to_stable_503(self) -> None:
        request = auth_api.LoginRequest(
            email="alice@example.com",
            password="correct-horse",
        )
        response = Response()
        with (
            patch("auth_api.authenticate_user", return_value={"id": "user-a"}),
            patch(
                "auth_api.create_session",
                side_effect=AuthStateError(
                    "authentication state /state/auth_sessions.json cannot be written"
                ),
            ),
        ):
            self.assert_state_unavailable(lambda: auth_api.login(request, response))

    def test_logout_maps_session_revoke_state_error_to_stable_503(self) -> None:
        response = Response()
        with patch(
            "auth_api.revoke_session",
            side_effect=AuthStateError(
                "authentication state /state/auth_sessions.json cannot be written"
            ),
        ):
            self.assert_state_unavailable(
                lambda: auth_api.logout(
                    response,
                    session_token="session-token",
                    _csrf=None,
                )
            )

    def test_register_input_auth_error_remains_422(self) -> None:
        request = auth_api.RegisterRequest(
            email="alice@example.com",
            password="correct-horse",
        )
        with patch("auth_api.register_user", side_effect=AuthError("invalid request")):
            with self.assertRaises(HTTPException) as raised:
                auth_api.register(request)
        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(raised.exception.detail, "invalid request")

    def test_invalid_credentials_remain_401(self) -> None:
        request = auth_api.LoginRequest(
            email="alice@example.com",
            password="wrong-password",
        )
        response = Response()
        with patch(
            "auth_api.authenticate_user",
            side_effect=InvalidCredentials("invalid email or password"),
        ):
            with self.assertRaises(HTTPException) as raised:
                auth_api.login(request, response)
        self.assertEqual(raised.exception.status_code, 401)
        self.assertEqual(raised.exception.detail, "invalid email or password")


if __name__ == "__main__":
    unittest.main()
