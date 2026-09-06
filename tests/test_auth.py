from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

# Point the store at a throwaway STATE_DIR before importing.
_TMP = tempfile.mkdtemp(prefix="irlight-auth-")
os.environ["STATE_DIR"] = _TMP

from auth_store import (  # noqa: E402
    AUTH_SESSIONS_PATH,
    AuthError,
    AuthStateError,
    EmailAlreadyRegistered,
    InvalidCredentials,
    PBKDF2_ITERATIONS,
    USERS_PATH,
    _hash_password,
    _verify_password,
    atomic_write_json,
    authenticate_user,
    create_session,
    ensure_auth_state,
    get_session_user,
    get_user,
    register_user,
    revoke_session,
)
from state_safety import initialization_marker  # noqa: E402


class AuthStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        for path in (USERS_PATH, AUTH_SESSIONS_PATH):
            if path.exists():
                path.unlink()
            initialization_marker(path).unlink(missing_ok=True)
        ensure_auth_state()

    def _register(
        self, email: str = "alice@example.com", password: str = "correct-horse"
    ) -> dict[str, object]:
        return register_user(email=email, password=password, display_name="Alice")

    def test_register_returns_no_password_hash(self) -> None:
        user = self._register()
        self.assertNotIn("password_hash", user)
        self.assertEqual(user["email"], "alice@example.com")
        self.assertEqual(user["display_name"], "Alice")
        self.assertEqual(user["role"], "user")

    def test_register_normalizes_email_case_and_whitespace(self) -> None:
        self._register(email="  Alice@Example.com  ")
        with self.assertRaises(EmailAlreadyRegistered):
            self._register(email="alice@example.com")

    def test_concurrent_duplicate_registration_has_one_winner(self) -> None:
        users: list[dict[str, object]] = []
        failures: list[BaseException] = []

        def run() -> None:
            try:
                users.append(self._register())
            except BaseException as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(len(users), 1)
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], EmailAlreadyRegistered)

    def test_corrupt_user_state_fails_closed(self) -> None:
        USERS_PATH.write_text("[]", encoding="utf-8")
        with self.assertRaises(AuthStateError):
            get_user("user-a")

    def test_initialized_user_state_deletion_fails_closed(self) -> None:
        self._register()
        USERS_PATH.unlink()
        with self.assertRaises(AuthStateError):
            self._register(email="bob@example.com")

    def test_user_record_missing_required_field_fails_closed(self) -> None:
        user = self._register()
        state = json.loads(USERS_PATH.read_text(encoding="utf-8"))
        state["users"][str(user["id"])].pop("password_hash")
        USERS_PATH.write_text(json.dumps(state), encoding="utf-8")

        with self.assertRaises(AuthStateError):
            get_user(str(user["id"]))

    def test_user_email_index_mismatch_fails_closed(self) -> None:
        user = self._register()
        state = json.loads(USERS_PATH.read_text(encoding="utf-8"))
        state["email_index"]["alice@example.com"] = "other-user"
        USERS_PATH.write_text(json.dumps(state), encoding="utf-8")

        with self.assertRaises(AuthStateError):
            get_user(str(user["id"]))

    def test_user_timestamp_rejects_bool_and_non_finite_value(self) -> None:
        user = self._register()
        original = json.loads(USERS_PATH.read_text(encoding="utf-8"))
        for bad_value in (True, None, "123", float("inf")):
            with self.subTest(bad_value=bad_value):
                state = json.loads(json.dumps(original))
                state["users"][str(user["id"])]["updated_at"] = bad_value
                USERS_PATH.write_text(json.dumps(state), encoding="utf-8")
                with self.assertRaises(AuthStateError):
                    get_user(str(user["id"]))

    def test_register_rejects_invalid_email(self) -> None:
        with self.assertRaises(AuthError):
            self._register(email="not-an-email")

    def test_register_rejects_short_password(self) -> None:
        with self.assertRaises(AuthError):
            self._register(password="short")

    def test_authenticate_success(self) -> None:
        self._register()
        user = authenticate_user(email="alice@example.com", password="correct-horse")
        self.assertEqual(user["email"], "alice@example.com")

    def test_authenticate_wrong_password_rejected(self) -> None:
        self._register()
        with self.assertRaises(InvalidCredentials):
            authenticate_user(email="alice@example.com", password="wrong-password")

    def test_authenticate_unknown_email_rejected(self) -> None:
        with self.assertRaises(InvalidCredentials):
            authenticate_user(email="nobody@example.com", password="whatever1")

    def test_password_hash_uses_a_random_salt(self) -> None:
        first = _hash_password("correct-horse")
        second = _hash_password("correct-horse")
        self.assertNotEqual(first, second)
        self.assertTrue(_verify_password("correct-horse", first))
        self.assertTrue(_verify_password("correct-horse", second))
        self.assertFalse(_verify_password("wrong", first))

    def test_password_hash_rejects_unbounded_work_factor_before_pbkdf2(self) -> None:
        user = self._register()
        state = json.loads(USERS_PATH.read_text(encoding="utf-8"))
        record = state["users"][str(user["id"])]
        algorithm, _iterations, salt_hex, digest_hex = record["password_hash"].split("$")
        record["password_hash"] = (
            f"{algorithm}${PBKDF2_ITERATIONS * 1000}${salt_hex}${digest_hex}"
        )
        USERS_PATH.write_text(json.dumps(state), encoding="utf-8")

        with patch("auth_store.hashlib.pbkdf2_hmac") as pbkdf2:
            with self.assertRaises(AuthStateError):
                authenticate_user(email="alice@example.com", password="correct-horse")
        pbkdf2.assert_not_called()

    def test_verify_password_rejects_noncanonical_parameters_without_pbkdf2(self) -> None:
        valid = _hash_password("correct-horse", salt=b"\x01" * 16)
        algorithm, _iterations, salt_hex, digest_hex = valid.split("$")
        unsupported = f"{algorithm}$999999999${salt_hex}${digest_hex}"
        oversized_salt = (
            f"{algorithm}${PBKDF2_ITERATIONS}${salt_hex * 2}${digest_hex}"
        )

        with patch("auth_store.hashlib.pbkdf2_hmac") as pbkdf2:
            self.assertFalse(_verify_password("correct-horse", unsupported))
            self.assertFalse(_verify_password("correct-horse", oversized_salt))
        pbkdf2.assert_not_called()

    def test_create_and_resolve_session(self) -> None:
        user = self._register()
        session = create_session(str(user["id"]))
        resolved = get_session_user(session["token"])
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["user"]["id"], user["id"])
        self.assertEqual(resolved["csrf_token"], session["csrf_token"])

    def test_unknown_token_resolves_to_none(self) -> None:
        self.assertIsNone(get_session_user("not-a-real-token"))

    def test_expired_session_resolves_to_none(self) -> None:
        user = self._register()
        session = create_session(str(user["id"]), ttl_seconds=-1)
        self.assertIsNone(get_session_user(session["token"]))

    def test_session_expiring_exactly_now_is_expired(self) -> None:
        user = self._register()
        with patch("auth_store.time.time", return_value=1_000.0):
            session = create_session(str(user["id"]), ttl_seconds=0)
            self.assertIsNone(get_session_user(session["token"]))

    def test_auth_session_json_rejects_non_finite_constants(self) -> None:
        for constant in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(constant=constant):
                AUTH_SESSIONS_PATH.write_text(
                    '{"sessions":{"token-hash":{"user_id":"user-a",'
                    '"csrf_token":"csrf","created_at":1.0,'
                    f'"expires_at":{constant}}}}}',
                    encoding="utf-8",
                )
                with self.assertRaises(AuthStateError):
                    get_session_user("unused-token")

    def test_auth_session_record_rejects_invalid_expiry_types(self) -> None:
        for bad_value in (True, None, "123"):
            with self.subTest(bad_value=bad_value):
                AUTH_SESSIONS_PATH.write_text(
                    json.dumps(
                        {
                            "sessions": {
                                "token-hash": {
                                    "user_id": "user-a",
                                    "csrf_token": "csrf",
                                    "created_at": 1.0,
                                    "expires_at": bad_value,
                                }
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                with self.assertRaises(AuthStateError):
                    get_session_user("unused-token")

    def test_auth_session_missing_csrf_token_fails_closed(self) -> None:
        AUTH_SESSIONS_PATH.write_text(
            json.dumps(
                {
                    "sessions": {
                        "token-hash": {
                            "user_id": "user-a",
                            "created_at": 1.0,
                            "expires_at": 2.0,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(AuthStateError):
            get_session_user("unused-token")

    def test_atomic_write_rejects_non_finite_json_without_replacing_state(self) -> None:
        before = USERS_PATH.read_text(encoding="utf-8")
        with self.assertRaises(AuthStateError):
            atomic_write_json(USERS_PATH, {"value": float("nan")})
        self.assertEqual(USERS_PATH.read_text(encoding="utf-8"), before)

    def test_revoked_session_resolves_to_none(self) -> None:
        user = self._register()
        session = create_session(str(user["id"]))
        revoke_session(session["token"])
        self.assertIsNone(get_session_user(session["token"]))

    def test_each_session_gets_a_distinct_token_and_csrf_token(self) -> None:
        user = self._register()
        first = create_session(str(user["id"]))
        second = create_session(str(user["id"]))
        self.assertNotEqual(first["token"], second["token"])
        self.assertNotEqual(first["csrf_token"], second["csrf_token"])

    def test_get_user_returns_public_fields_only(self) -> None:
        user = self._register()
        fetched = get_user(str(user["id"]))
        self.assertIsNotNone(fetched)
        self.assertNotIn("password_hash", fetched)


if __name__ == "__main__":
    unittest.main()
