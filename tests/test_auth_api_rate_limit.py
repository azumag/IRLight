from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import HTTPException, Response
from starlette.requests import Request as HttpRequest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

import auth_api  # noqa: E402
from auth_rate_limit import AuthRateLimitUnavailable  # noqa: E402
from auth_store import AuthStateError, InvalidCredentials  # noqa: E402


def _http_request(
    address: str | None = "192.0.2.10",
    forwarded_for: str | None = None,
) -> HttpRequest:
    headers = []
    if forwarded_for is not None:
        headers.append((b"x-forwarded-for", forwarded_for.encode("ascii")))
    scope: dict[str, object] = {"type": "http", "headers": headers}
    if address is not None:
        scope["client"] = (address, 12345)
    return HttpRequest(scope)


REGISTER = auth_api.RegisterRequest(email="alice@example.com", password="correct-horse")
LOGIN = auth_api.LoginRequest(email="alice@example.com", password="correct-horse")


class AuthApiRateLimitTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(prefix="irlight-auth-rate-limit-")
        self.addCleanup(self._temp_dir.cleanup)
        os.environ.pop("IRLIGHT_TRUSTED_PROXIES", None)
        env = {
            "IRLIGHT_AUTH_RATE_LIMIT_DIR": self._temp_dir.name,
            "IRLIGHT_AUTH_RATE_LIMIT_BURST_LIMIT": "1",
            "IRLIGHT_AUTH_RATE_LIMIT_BURST_WINDOW_SECONDS": "60",
            "IRLIGHT_AUTH_RATE_LIMIT_SUSTAINED_LIMIT": "1",
            "IRLIGHT_AUTH_RATE_LIMIT_SUSTAINED_WINDOW_SECONDS": "60",
        }
        self._env = patch.dict(os.environ, env, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_register_is_limited_with_a_retry_after_header(self) -> None:
        with patch("auth_api.register_user", return_value={"id": "user-a"}):
            self.assertEqual(
                auth_api.register(REGISTER, _http_request())["user"], {"id": "user-a"}
            )
            with self.assertRaises(HTTPException) as raised:
                auth_api.register(REGISTER, _http_request())
        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(
            raised.exception.detail, {"code": auth_api.AUTH_RATE_LIMITED_CODE}
        )
        self.assertEqual(raised.exception.headers, {"Retry-After": "60"})

    def test_login_is_limited_with_a_retry_after_header(self) -> None:
        response = Response()
        with patch(
            "auth_api.authenticate_user", side_effect=InvalidCredentials("nope")
        ):
            with self.assertRaises(HTTPException) as raised:
                auth_api.login(LOGIN, response, _http_request())
            self.assertEqual(raised.exception.status_code, 401)
            with self.assertRaises(HTTPException) as raised:
                auth_api.login(LOGIN, response, _http_request())
        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(
            raised.exception.detail, {"code": auth_api.AUTH_RATE_LIMITED_CODE}
        )

    def test_limited_response_does_not_reveal_account_existence(self) -> None:
        known = auth_api.LoginRequest(email="known@example.com", password="secret-1")
        unknown = auth_api.LoginRequest(email="unknown@example.com", password="secret-2")
        response = Response()
        with patch(
            "auth_api.authenticate_user", side_effect=InvalidCredentials("nope")
        ):
            for request in (known, unknown):
                with self.assertRaises(HTTPException) as raised:
                    auth_api.login(request, response, _http_request())
                self.assertEqual(raised.exception.status_code, 401)
            observed = []
            for request in (known, unknown):
                with self.assertRaises(HTTPException) as raised:
                    auth_api.login(request, response, _http_request())
                observed.append(
                    (
                        raised.exception.status_code,
                        json.dumps(raised.exception.detail, sort_keys=True),
                        json.dumps(raised.exception.headers, sort_keys=True),
                    )
                )
        self.assertEqual(observed[0], observed[1])
        self.assertNotIn("example.com", observed[0][1])

    def test_rejected_attempt_never_takes_a_password_kdf_slot(self) -> None:
        response = Response()
        slot = MagicMock()
        slot.return_value.__enter__.return_value = None
        with (
            patch("auth_api.auth_kdf_slot", slot),
            patch(
                "auth_api.authenticate_user",
                return_value={"id": "user-a"},
            ),
            patch(
                "auth_api.create_session",
                return_value={"token": "token", "csrf_token": "csrf"},
            ),
        ):
            auth_api.login(LOGIN, response, _http_request())
            with self.assertRaises(HTTPException) as raised:
                auth_api.login(LOGIN, response, _http_request())
        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(slot.call_count, 1)

    def test_unusable_admission_boundary_fails_closed_with_a_stable_code(self) -> None:
        with patch(
            "auth_api.enforce_authentication_rate_limit",
            side_effect=AuthRateLimitUnavailable("unavailable"),
        ):
            for callback in (
                lambda: auth_api.register(REGISTER, _http_request()),
                lambda: auth_api.login(LOGIN, Response(), _http_request()),
            ):
                with self.assertRaises(HTTPException) as raised:
                    callback()
                self.assertEqual(raised.exception.status_code, 503)
                self.assertEqual(
                    raised.exception.detail,
                    {"code": auth_api.AUTH_RATE_LIMIT_UNAVAILABLE_CODE},
                )
                self.assertNotEqual(
                    raised.exception.detail,
                    {"code": auth_api.AUTH_STATE_UNAVAILABLE_CODE},
                )

    def test_unexpected_admission_errors_are_not_masked(self) -> None:
        # Only the limiter's own outcomes may be translated into 429/503; any
        # other failure keeps propagating instead of being reported as a limit.
        with patch(
            "auth_api.enforce_authentication_rate_limit",
            side_effect=AuthStateError("authentication state is corrupt"),
        ):
            with self.assertRaises(AuthStateError):
                auth_api.register(REGISTER, _http_request())

    def test_trusted_proxy_header_is_used_for_the_admission_key(self) -> None:
        with patch.dict(
            os.environ, {"IRLIGHT_TRUSTED_PROXIES": "192.0.2.10"}, clear=False
        ):
            with patch("auth_api.register_user", return_value={"id": "user-a"}):
                auth_api.register(REGISTER, _http_request(forwarded_for="203.0.113.7"))
                auth_api.register(REGISTER, _http_request(forwarded_for="203.0.113.8"))
                with self.assertRaises(HTTPException) as raised:
                    auth_api.register(REGISTER, _http_request(forwarded_for="203.0.113.7"))
        self.assertEqual(raised.exception.status_code, 429)

    def test_untrusted_forwarded_header_cannot_split_the_limit(self) -> None:
        with patch("auth_api.register_user", return_value={"id": "user-a"}):
            auth_api.register(REGISTER, _http_request(forwarded_for="203.0.113.7"))
            with self.assertRaises(HTTPException) as raised:
                auth_api.register(REGISTER, _http_request(forwarded_for="203.0.113.8"))
        self.assertEqual(raised.exception.status_code, 429)

    def test_distinct_clients_keep_separate_limits(self) -> None:
        with patch("auth_api.register_user", return_value={"id": "user-a"}):
            auth_api.register(REGISTER, _http_request("192.0.2.10"))
            auth_api.register(REGISTER, _http_request("192.0.2.11"))


if __name__ == "__main__":
    unittest.main()
