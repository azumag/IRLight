from __future__ import annotations

import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException, Response


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

import auth_api  # noqa: E402
from auth_kdf_admission import (  # noqa: E402
    DEFAULT_ADMISSION_DIR,
    DEFAULT_MAX_CONCURRENT_KDFS,
    AuthKdfAdmissionBusy,
    AuthKdfAdmissionConfig,
    AuthKdfAdmissionUnavailable,
    auth_kdf_slot,
)


class AuthKdfAdmissionTest(unittest.TestCase):
    def test_invalid_environment_limit_falls_back_to_bounded_default(self) -> None:
        for value in ("0", "33", "not-a-number"):
            with self.subTest(value=value):
                with patch.dict(
                    os.environ,
                    {"IRLIGHT_AUTH_KDF_MAX_CONCURRENT": value},
                    clear=False,
                ):
                    config = AuthKdfAdmissionConfig.from_env()
                self.assertEqual(config.max_concurrent, DEFAULT_MAX_CONCURRENT_KDFS)

    def test_environment_accepts_bounded_limit_and_absolute_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-auth-kdf-config-") as temp_dir:
            with patch.dict(
                os.environ,
                {
                    "IRLIGHT_AUTH_KDF_MAX_CONCURRENT": "1",
                    "IRLIGHT_AUTH_KDF_ADMISSION_DIR": temp_dir,
                },
                clear=False,
            ):
                config = AuthKdfAdmissionConfig.from_env()

        self.assertEqual(config.max_concurrent, 1)
        self.assertEqual(config.lock_dir, Path(temp_dir))

    def test_relative_environment_directory_falls_back_to_safe_default(self) -> None:
        with patch.dict(
            os.environ,
            {"IRLIGHT_AUTH_KDF_ADMISSION_DIR": "relative/locks"},
            clear=False,
        ):
            config = AuthKdfAdmissionConfig.from_env()
        self.assertEqual(config.lock_dir, Path(DEFAULT_ADMISSION_DIR))

    def test_second_holder_is_rejected_and_slot_is_reusable_after_release(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-auth-kdf-slots-") as temp_dir:
            config = AuthKdfAdmissionConfig(max_concurrent=1, lock_dir=Path(temp_dir))
            with auth_kdf_slot(config):
                with self.assertRaises(AuthKdfAdmissionBusy):
                    with auth_kdf_slot(config):
                        self.fail("saturated admission unexpectedly yielded")

            # Kernel/file-descriptor release must make the same slot reusable.
            with auth_kdf_slot(config):
                pass

    def test_symlink_admission_directory_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-auth-kdf-parent-") as parent:
            root = Path(parent)
            actual = root / "actual"
            actual.mkdir()
            alias = root / "admission"
            alias.symlink_to(actual, target_is_directory=True)
            config = AuthKdfAdmissionConfig(max_concurrent=1, lock_dir=alias)

            with self.assertRaises(AuthKdfAdmissionUnavailable):
                with auth_kdf_slot(config):
                    self.fail("symlink admission directory unexpectedly yielded")

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "requires O_NOFOLLOW")
    def test_symlink_slot_file_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-auth-kdf-slot-link-") as temp_dir:
            lock_dir = Path(temp_dir)
            target = lock_dir / "target"
            target.write_text("", encoding="utf-8")
            (lock_dir / "slot-0.lock").symlink_to(target)
            config = AuthKdfAdmissionConfig(max_concurrent=1, lock_dir=lock_dir)

            with self.assertRaises(AuthKdfAdmissionUnavailable):
                with auth_kdf_slot(config):
                    self.fail("symlink slot unexpectedly yielded")


class AuthKdfAdmissionApiTest(unittest.TestCase):
    def test_register_busy_is_retryable_503_without_invoking_password_work(self) -> None:
        request = auth_api.RegisterRequest(
            email="alice@example.com",
            password="correct-horse",
        )
        with (
            patch(
                "auth_api.auth_kdf_slot",
                side_effect=AuthKdfAdmissionBusy("capacity busy"),
            ),
            patch("auth_api.register_user") as register_user,
        ):
            with self.assertRaises(HTTPException) as raised:
                auth_api.register(request)

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(
            raised.exception.detail,
            {"code": auth_api.AUTH_COMPUTE_BUSY_CODE},
        )
        self.assertEqual(raised.exception.headers, {"Retry-After": "1"})
        register_user.assert_not_called()

    def test_login_unavailable_fails_closed_without_invoking_password_work(self) -> None:
        request = auth_api.LoginRequest(
            email="alice@example.com",
            password="correct-horse",
        )
        response = Response()
        with (
            patch(
                "auth_api.auth_kdf_slot",
                side_effect=AuthKdfAdmissionUnavailable("unsafe lock set"),
            ),
            patch("auth_api.authenticate_user") as authenticate_user,
            patch("auth_api.create_session") as create_session,
        ):
            with self.assertRaises(HTTPException) as raised:
                auth_api.login(request, response)

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(
            raised.exception.detail,
            {"code": auth_api.AUTH_COMPUTE_UNAVAILABLE_CODE},
        )
        self.assertIsNone(raised.exception.headers)
        authenticate_user.assert_not_called()
        create_session.assert_not_called()

    def test_login_releases_compute_slot_before_session_issuance(self) -> None:
        held = False

        @contextmanager
        def fake_slot():
            nonlocal held
            self.assertFalse(held)
            held = True
            try:
                yield
            finally:
                held = False

        def create_session(user_id: str, *, ttl_seconds: int):
            self.assertFalse(held)
            self.assertEqual(user_id, "user-a")
            self.assertEqual(ttl_seconds, auth_api.SESSION_TTL_SECONDS)
            return {
                "token": "session-token",
                "csrf_token": "csrf-token",
                "expires_at": 1.0,
            }

        request = auth_api.LoginRequest(
            email="alice@example.com",
            password="correct-horse",
        )
        response = Response()
        with (
            patch("auth_api.auth_kdf_slot", side_effect=fake_slot),
            patch("auth_api.authenticate_user", return_value={"id": "user-a"}),
            patch("auth_api.create_session", side_effect=create_session),
        ):
            result = auth_api.login(request, response)

        self.assertFalse(held)
        self.assertEqual(result["user"], {"id": "user-a"})
        self.assertEqual(result["csrf_token"], "csrf-token")


if __name__ == "__main__":
    unittest.main()
