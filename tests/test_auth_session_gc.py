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

# Keep the auth_store process/file lock in a writable throwaway directory when
# this test module is imported before the broader auth test module.
_TMP = tempfile.mkdtemp(prefix="irlight-auth-gc-")
os.environ.setdefault("STATE_DIR", _TMP)

from auth_session_gc import (  # noqa: E402
    MAX_DELETIONS_PER_RUN,
    _expired_token_hashes,
    prune_expired_sessions,
)
from auth_store import AuthStateError  # noqa: E402


class AuthSessionGcTest(unittest.TestCase):
    @staticmethod
    def _token_hash(value: int) -> str:
        return f"{value:064x}"

    @staticmethod
    def _record(*, expires_at: float) -> dict[str, object]:
        return {
            "user_id": "user-a",
            "csrf_token": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "created_at": 1.0,
            "expires_at": expires_at,
        }

    def test_expiry_boundary_and_limit_are_deterministic(self) -> None:
        state = {
            "sessions": {
                self._token_hash(4): self._record(expires_at=100.0),
                self._token_hash(2): self._record(expires_at=10.0),
                self._token_hash(1): self._record(expires_at=10.0),
                self._token_hash(3): self._record(expires_at=101.0),
            }
        }
        selected, expired_count = _expired_token_hashes(
            state, now=100.0, max_deletions=2
        )
        self.assertEqual(expired_count, 3)
        self.assertEqual(selected, [self._token_hash(1), self._token_hash(2)])

    def test_prune_deletes_only_expired_records_and_reports_remaining(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-auth-gc-state-") as tmp:
            path = Path(tmp) / "auth_sessions.json"
            path.write_text(
                json.dumps(
                    {
                        "sessions": {
                            self._token_hash(1): self._record(expires_at=10.0),
                            self._token_hash(2): self._record(expires_at=20.0),
                            self._token_hash(3): self._record(expires_at=200.0),
                        }
                    }
                ),
                encoding="utf-8",
            )

            result = prune_expired_sessions(
                now=100.0, max_deletions=1, path=path
            )
            state = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(result.scanned, 3)
        self.assertEqual(result.deleted, 1)
        self.assertEqual(result.expired_remaining, 1)
        self.assertEqual(
            set(state["sessions"]), {self._token_hash(2), self._token_hash(3)}
        )

    def test_dry_run_does_not_rewrite_authority(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-auth-gc-state-") as tmp:
            path = Path(tmp) / "auth_sessions.json"
            original = json.dumps(
                {"sessions": {self._token_hash(1): self._record(expires_at=10.0)}}
            )
            path.write_text(original, encoding="utf-8")

            with patch("auth_session_gc.atomic_write_json") as writer:
                result = prune_expired_sessions(
                    now=100.0, max_deletions=10, dry_run=True, path=path
                )

            self.assertEqual(path.read_text(encoding="utf-8"), original)
            writer.assert_not_called()

        self.assertEqual(result.deleted, 0)
        self.assertEqual(result.expired_remaining, 1)
        self.assertTrue(result.dry_run)

    def test_no_expired_records_avoids_authority_write(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-auth-gc-state-") as tmp:
            path = Path(tmp) / "auth_sessions.json"
            path.write_text(
                json.dumps(
                    {"sessions": {self._token_hash(1): self._record(expires_at=200.0)}}
                ),
                encoding="utf-8",
            )

            with patch("auth_session_gc.atomic_write_json") as writer:
                result = prune_expired_sessions(
                    now=100.0, max_deletions=10, path=path
                )

            writer.assert_not_called()

        self.assertEqual(result.deleted, 0)
        self.assertEqual(result.expired_remaining, 0)

    def test_corrupt_state_fails_closed_without_rewrite(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-auth-gc-state-") as tmp:
            path = Path(tmp) / "auth_sessions.json"
            original = {
                "sessions": {
                    self._token_hash(1): {
                        "user_id": "user-a",
                        "csrf_token": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                        "created_at": 1.0,
                        "expires_at": "100",
                    }
                }
            }
            path.write_text(json.dumps(original), encoding="utf-8")

            with patch("auth_session_gc.atomic_write_json") as writer:
                with self.assertRaises(AuthStateError):
                    prune_expired_sessions(now=200.0, path=path)
                writer.assert_not_called()

            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")), original
            )

    def test_invalid_token_hash_fails_closed_without_rewrite(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-auth-gc-state-") as tmp:
            path = Path(tmp) / "auth_sessions.json"
            original = {"sessions": {"not-a-digest": self._record(expires_at=10.0)}}
            path.write_text(json.dumps(original), encoding="utf-8")

            with patch("auth_session_gc.atomic_write_json") as writer:
                with self.assertRaisesRegex(AuthStateError, "invalid token hash"):
                    prune_expired_sessions(now=200.0, path=path)
                writer.assert_not_called()

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), original)

    def test_invalid_csrf_token_fails_closed_without_rewrite(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-auth-gc-state-") as tmp:
            path = Path(tmp) / "auth_sessions.json"
            record = self._record(expires_at=10.0)
            record["csrf_token"] = "not-a-writer-token"
            original = {"sessions": {self._token_hash(1): record}}
            path.write_text(json.dumps(original), encoding="utf-8")

            with patch("auth_session_gc.atomic_write_json") as writer:
                with self.assertRaisesRegex(AuthStateError, "invalid csrf_token"):
                    prune_expired_sessions(now=200.0, path=path)
                writer.assert_not_called()

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), original)

    def test_max_delete_and_now_must_be_bounded_and_finite(self) -> None:
        with self.assertRaises(ValueError):
            prune_expired_sessions(max_deletions=0)
        with self.assertRaises(ValueError):
            prune_expired_sessions(max_deletions=MAX_DELETIONS_PER_RUN + 1)
        with self.assertRaises(ValueError):
            prune_expired_sessions(now=float("inf"))
        with self.assertRaises(ValueError):
            prune_expired_sessions(now=float("nan"))


if __name__ == "__main__":
    unittest.main()
