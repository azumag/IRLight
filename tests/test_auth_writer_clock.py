from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CONTROL_API_DIR = ROOT / "apps" / "control-api"
sys.path.insert(0, str(CONTROL_API_DIR))


def _load_auth_store(state_dir: str) -> types.ModuleType:
    previous = os.environ.get("STATE_DIR")
    os.environ["STATE_DIR"] = state_dir
    try:
        spec = importlib.util.spec_from_file_location(
            f"irlight_auth_writer_clock_{id(state_dir)}",
            CONTROL_API_DIR / "auth_store.py",
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("failed to load auth_store")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(spec.name, None)
        return module
    finally:
        if previous is None:
            os.environ.pop("STATE_DIR", None)
        else:
            os.environ["STATE_DIR"] = previous


class AuthWriterClockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="irlight-auth-clock-")
        self.auth = _load_auth_store(self.temporary.name)
        self.auth.ensure_auth_state()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_register_rejects_invalid_clock_before_expensive_work_or_state_change(self) -> None:
        before = self.auth.USERS_PATH.read_bytes()
        for invalid in (-1.0, float("nan"), float("inf"), float("-inf"), 10**10000):
            with self.subTest(invalid=type(invalid).__name__):
                with (
                    mock.patch.object(self.auth.time, "time", return_value=invalid),
                    mock.patch.object(self.auth.hashlib, "pbkdf2_hmac") as pbkdf2,
                ):
                    with self.assertRaisesRegex(
                        self.auth.AuthStateError, "authentication clock is invalid"
                    ):
                        self.auth.register_user(
                            email="alice@example.com", password="correct-horse"
                        )
                pbkdf2.assert_not_called()
                self.assertEqual(self.auth.USERS_PATH.read_bytes(), before)

    def test_create_session_rejects_invalid_clock_before_token_or_state_change(self) -> None:
        before = self.auth.AUTH_SESSIONS_PATH.read_bytes()
        for invalid in (-1.0, float("nan"), float("inf"), float("-inf"), 10**10000):
            with self.subTest(invalid=type(invalid).__name__):
                with (
                    mock.patch.object(self.auth.time, "time", return_value=invalid),
                    mock.patch.object(self.auth.secrets, "token_urlsafe") as token_urlsafe,
                ):
                    with self.assertRaisesRegex(
                        self.auth.AuthStateError, "authentication clock is invalid"
                    ):
                        self.auth.create_session("user-a")
                token_urlsafe.assert_not_called()
                self.assertEqual(self.auth.AUTH_SESSIONS_PATH.read_bytes(), before)

    def test_user_reader_rejects_negative_created_at_without_rewriting_authority(self) -> None:
        with mock.patch.object(self.auth.time, "time", return_value=1_000.0):
            user = self.auth.register_user(
                email="alice@example.com", password="correct-horse"
            )
        state = json.loads(self.auth.USERS_PATH.read_text(encoding="utf-8"))
        record = state["users"][str(user["id"])]
        record["created_at"] = -1.0
        record["updated_at"] = 1_000.0
        damaged = json.dumps(state, sort_keys=True)
        self.auth.USERS_PATH.write_text(damaged, encoding="utf-8")

        with self.assertRaisesRegex(self.auth.AuthStateError, "invalid created_at"):
            self.auth.get_user(str(user["id"]))
        self.assertEqual(self.auth.USERS_PATH.read_text(encoding="utf-8"), damaged)

    def test_epoch_zero_remains_a_valid_writer_clock(self) -> None:
        with mock.patch.object(self.auth.time, "time", return_value=0.0):
            user = self.auth.register_user(
                email="alice@example.com", password="correct-horse"
            )
            session = self.auth.create_session(str(user["id"]), ttl_seconds=10)

        self.assertEqual(user["created_at"], 0.0)
        self.assertEqual(user["updated_at"], 0.0)
        self.assertEqual(session["expires_at"], 10.0)

    def test_negative_request_clock_fails_before_session_authority_read(self) -> None:
        with (
            mock.patch.object(self.auth.time, "time", return_value=-1.0),
            mock.patch.object(self.auth, "read_json") as read_json,
        ):
            with self.assertRaisesRegex(
                self.auth.AuthStateError, "authentication clock is invalid"
            ):
                self.auth.get_session_user("unused-token")
        read_json.assert_not_called()


if __name__ == "__main__":
    unittest.main()
