from __future__ import annotations

import contextlib
import io
import json
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from auth_session_retention_inspect_cli import (  # noqa: E402
    RetentionInspectError,
    inspect_auth_session_retention,
    main,
)


CSRF = "A" * 32


def session_record(*, expires_at: float, created_at: float = 1.0) -> dict[str, object]:
    return {
        "user_id": "user-1",
        "csrf_token": CSRF,
        "created_at": created_at,
        "expires_at": expires_at,
    }


class AuthSessionRetentionInspectTests(unittest.TestCase):
    def test_counts_expiry_boundary_and_estimates_bounded_gc_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth_sessions.json"
            path.write_text(
                json.dumps(
                    {
                        "sessions": {
                            "a" * 64: session_record(expires_at=99.0),
                            "b" * 64: session_record(expires_at=100.0),
                            "c" * 64: session_record(expires_at=101.0),
                        }
                    }
                ),
                encoding="utf-8",
            )

            snapshot = inspect_auth_session_retention(
                path=path,
                now=100.0,
                max_deletions=1,
            )

            self.assertEqual(snapshot.sessions_total, 3)
            self.assertEqual(snapshot.sessions_expired, 2)
            self.assertEqual(snapshot.sessions_active, 1)
            self.assertEqual(snapshot.gc_runs_required, 2)

    def test_inspection_does_not_create_lock_marker_or_rewrite_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "auth_sessions.json"
            content = json.dumps(
                {"sessions": {"d" * 64: session_record(expires_at=200.0)}}
            )
            path.write_text(content, encoding="utf-8")
            before_entries = sorted(item.name for item in root.iterdir())
            before_stat = path.stat()

            inspect_auth_session_retention(path=path, now=100.0)

            after_entries = sorted(item.name for item in root.iterdir())
            after_stat = path.stat()
            self.assertEqual(after_entries, before_entries)
            self.assertEqual(path.read_text(encoding="utf-8"), content)
            self.assertEqual(after_stat.st_mtime_ns, before_stat.st_mtime_ns)
            self.assertEqual(after_stat.st_size, before_stat.st_size)

    def test_strict_json_and_record_validation_fail_closed(self) -> None:
        invalid_documents = (
            '{"sessions":{"' + "a" * 64 + '":{"user_id":"u","csrf_token":"' + CSRF + '","created_at":1,"expires_at":NaN}}}',
            '{"sessions":{},"sessions":{}}',
            json.dumps(
                {
                    "sessions": {
                        "a" * 64: {
                            "user_id": "u",
                            "csrf_token": CSRF,
                            "created_at": 1,
                        }
                    }
                }
            ),
            json.dumps(
                {"sessions": {"NOT-A-TOKEN-HASH": session_record(expires_at=2.0)}}
            ),
        )
        for document in invalid_documents:
            with self.subTest(document=document[:40]):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "auth_sessions.json"
                    path.write_text(document, encoding="utf-8")
                    with self.assertRaises(RetentionInspectError):
                        inspect_auth_session_retention(path=path, now=1.0)

    def test_non_finite_inspection_time_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth_sessions.json"
            path.write_text('{"sessions":{}}', encoding="utf-8")
            for value in (math.nan, math.inf, -math.inf):
                with self.subTest(value=value):
                    with self.assertRaises(RetentionInspectError):
                        inspect_auth_session_retention(path=path, now=value)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink is unavailable")
    def test_symlink_state_file_is_rejected_without_reading_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.json"
            target.write_text('{"sessions":{}}', encoding="utf-8")
            path = root / "auth_sessions.json"
            path.symlink_to(target)

            with self.assertRaises(RetentionInspectError):
                inspect_auth_session_retention(path=path, now=1.0)

    def test_cli_failure_returns_fixed_non_secret_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "AUDIT_DUMMY_SECRET.json"
            path.write_text('{"sessions":{"token":"AUDIT_DUMMY_SECRET"}}', encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                rc = main(["--state-file", str(path)])

            rendered = stdout.getvalue()
            self.assertEqual(rc, 2)
            self.assertEqual(
                json.loads(rendered),
                {
                    "status": "UNAVAILABLE",
                    "reason_code": "AUTH_SESSION_STATE_UNAVAILABLE",
                },
            )
            self.assertNotIn("AUDIT_DUMMY_SECRET", rendered)
            self.assertNotIn(str(path), rendered)

    def test_cli_success_exposes_only_aggregate_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth_sessions.json"
            token_hash = "f" * 64
            path.write_text(
                json.dumps(
                    {"sessions": {token_hash: session_record(expires_at=1.0)}}
                ),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                rc = main(["--state-file", str(path), "--max-delete", "10"])

            rendered = stdout.getvalue()
            payload = json.loads(rendered)
            self.assertEqual(rc, 0)
            self.assertEqual(payload["sessions_total"], 1)
            self.assertEqual(payload["sessions_expired"], 1)
            self.assertNotIn(token_hash, rendered)
            self.assertNotIn("user-1", rendered)
            self.assertNotIn(CSRF, rendered)


if __name__ == "__main__":
    unittest.main()
