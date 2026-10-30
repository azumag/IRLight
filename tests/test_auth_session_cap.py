from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

# Point the store at a throwaway STATE_DIR before importing (shared with the
# other auth tests; whichever test module imports auth_store first wins).
_TMP = tempfile.mkdtemp(prefix="irlight-auth-cap-")
os.environ.setdefault("STATE_DIR", _TMP)

from auth_store import (  # noqa: E402
    AUTH_SESSIONS_PATH,
    MAX_SESSIONS_PER_USER,
    USERS_PATH,
    create_session,
    ensure_auth_state,
    get_session_user,
    register_user,
)
from state_safety import initialization_marker  # noqa: E402


class AuthSessionCapTest(unittest.TestCase):
    def setUp(self) -> None:
        for path in (USERS_PATH, AUTH_SESSIONS_PATH):
            if path.exists():
                path.unlink()
            initialization_marker(path).unlink(missing_ok=True)
        ensure_auth_state()

    def _persisted_count(self) -> int:
        state = json.loads(AUTH_SESSIONS_PATH.read_text(encoding="utf-8"))
        return len(state["sessions"])

    def test_login_beyond_cap_revokes_oldest_live_sessions(self) -> None:
        """Issue #86: per-user retained sessions stay bounded."""
        user = register_user(email="cap@example.com", password="correct-horse")
        tokens: list[str] = []
        for index in range(MAX_SESSIONS_PER_USER + 3):
            with patch("auth_store.time.time", return_value=1000.0 + index):
                tokens.append(create_session(str(user["id"]))["token"])

        # The three oldest sessions were evicted; the newest cap-size set works.
        # Resolve under a clock just after creation so the live records have
        # not yet reached their default-TTL expiry.
        with patch("auth_store.time.time", return_value=1020.0):
            for index in range(3):
                self.assertIsNone(get_session_user(tokens[index]))
            for index in range(3, MAX_SESSIONS_PER_USER + 3):
                self.assertIsNotNone(get_session_user(tokens[index]))
        self.assertEqual(self._persisted_count(), MAX_SESSIONS_PER_USER)

    def test_cap_does_not_count_expired_sessions(self) -> None:
        user = register_user(email="expired@example.com", password="correct-horse")
        for index in range(MAX_SESSIONS_PER_USER + 5):
            with patch("auth_store.time.time", return_value=100.0 + index):
                create_session(str(user["id"]), ttl_seconds=0)
        # All previous records are expired; none of them count against the cap,
        # but they also keep existing until the bounded GC reclaims them.
        live_tokens: list[str] = []
        for index in range(MAX_SESSIONS_PER_USER):
            live = create_session(str(user["id"]))["token"]
            live_tokens.append(live)
            self.assertIsNotNone(get_session_user(live))

        # Only live records may be counted, so every live token still resolves
        # even though many expired records exist in the same authority file.
        for token in live_tokens:
            self.assertIsNotNone(get_session_user(token))

    def test_cap_is_per_user_and_does_not_evict_other_users(self) -> None:
        alice = register_user(email="a@example.com", password="correct-horse")
        bob = register_user(email="b@example.com", password="correct-horse")
        bob_session = create_session(str(bob["id"]))["token"]

        for index in range(MAX_SESSIONS_PER_USER + 2):
            with patch("auth_store.time.time", return_value=2000.0 + index):
                create_session(str(alice["id"]))

        self.assertIsNotNone(get_session_user(bob_session))
        self.assertEqual(self._persisted_count(), MAX_SESSIONS_PER_USER + 1)

    def test_eviction_order_is_oldest_created_at_first(self) -> None:
        user = register_user(email="order@example.com", password="correct-horse")
        created = []
        # Create a full cap; later sessions are newer.
        for index in range(MAX_SESSIONS_PER_USER):
            with patch("auth_store.time.time", return_value=5000.0 + index):
                created.append(create_session(str(user["id"]))["token"])
        with patch("auth_store.time.time", return_value=9999.0):
            newest = create_session(str(user["id"]))["token"]

        with patch("auth_store.time.time", return_value=10_000.0):
            self.assertIsNone(get_session_user(created[0]))
            for token in created[1:]:
                self.assertIsNotNone(get_session_user(token))
            self.assertIsNotNone(get_session_user(newest))

    def test_expiring_exactly_now_does_not_count_toward_cap(self) -> None:
        user = register_user(email="edge@example.com", password="correct-horse")
        with patch("auth_store.time.time", return_value=1000.0):
            expiring = create_session(str(user["id"]), ttl_seconds=1)
        # At expires_at == now the record is already expired, so it must not
        # count toward the live-session cap.
        with patch("auth_store.time.time", return_value=1001.0):
            for _ in range(MAX_SESSIONS_PER_USER):
                live = create_session(str(user["id"]))["token"]
                self.assertIsNotNone(get_session_user(live))
        self.assertIsNone(get_session_user(expiring["token"]))


if __name__ == "__main__":
    unittest.main()
