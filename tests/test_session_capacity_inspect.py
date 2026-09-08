from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTROL_API = ROOT / "apps" / "control-api"
if str(CONTROL_API) not in sys.path:
    sys.path.insert(0, str(CONTROL_API))

from session_capacity_inspect_cli import (  # noqa: E402
    SessionCapacityInspectError,
    _read_entitlements,
    _read_sessions,
    main,
    summarize_session_capacity,
)
from session_store import new_session  # noqa: E402


class SessionCapacityInspectTest(unittest.TestCase):
    def test_capacity_accounting_matches_session_store_capacity_states(self) -> None:
        sessions = {
            "active": {"user_id": "user-a", "status": "LIVE", "entitlement_reserved": False},
            "reserved": {"user_id": "user-a", "status": "STOPPED", "entitlement_reserved": True},
            "stopping": {"user_id": "user-a", "status": "STOPPING", "entitlement_reserved": False},
            "finished": {"user_id": "user-a", "status": "FINISHED", "entitlement_reserved": False},
            "other": {"user_id": "user-b", "status": "LIVE", "entitlement_reserved": True},
        }
        entitlements = {
            "user-a": {
                "id": "user:user-a",
                "user_id": "user-a",
                "plan": "test",
                "max_concurrent_sessions": 4,
                "updated_at": 1.0,
            }
        }

        summary = summarize_session_capacity(
            sessions, entitlements, user_id="user-a", default_limit=1
        )

        self.assertEqual(
            summary,
            {"status": "CAPACITY_AVAILABLE", "limit": 4, "occupied": 3, "available": 1},
        )

    def test_capacity_at_limit_is_exhausted(self) -> None:
        summary = summarize_session_capacity(
            {
                "one": {"user_id": "user-a", "status": "LIVE"},
                "two": {"user_id": "user-a", "status": "FAILED_CLEANUP"},
            },
            {
                "user-a": {
                    "id": "user:user-a",
                    "user_id": "user-a",
                    "plan": "test",
                    "max_concurrent_sessions": 2,
                    "updated_at": 1.0,
                }
            },
            user_id="user-a",
            default_limit=1,
        )
        self.assertEqual(summary["status"], "CAPACITY_EXHAUSTED")
        self.assertEqual(summary["available"], 0)

    def test_missing_entitlement_uses_runtime_default(self) -> None:
        summary = summarize_session_capacity(
            {}, {}, user_id="user-a", default_limit=0
        )
        self.assertEqual(
            summary,
            {"status": "CAPACITY_DISABLED", "limit": 0, "occupied": 0, "available": 0},
        )

    def test_readers_do_not_create_markers_or_lock_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            session = new_session(user_id="user-a")
            session_id = session["session_id"]
            (state_dir / "sessions.json").write_text(
                json.dumps({"sessions": {session_id: session}, "orphan_cleanup_leases": {}}),
                encoding="utf-8",
            )
            (state_dir / "entitlements.json").write_text(
                json.dumps(
                    {
                        "entitlements": {
                            "user-a": {
                                "id": "user:user-a",
                                "user_id": "user-a",
                                "plan": "test",
                                "max_concurrent_sessions": 1,
                                "updated_at": 1.0,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            before = {path.name: path.read_bytes() for path in state_dir.iterdir()}

            sessions = _read_sessions(state_dir)
            entitlements = _read_entitlements(state_dir)

            after = {path.name: path.read_bytes() for path in state_dir.iterdir()}
            self.assertEqual(before, after)
            self.assertEqual(set(sessions), {session_id})
            self.assertEqual(set(entitlements), {"user-a"})

    def test_initialized_missing_authority_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            (state_dir / ".sessions.json.initialized").write_text("v1\n", encoding="utf-8")
            with self.assertRaises(SessionCapacityInspectError):
                _read_sessions(state_dir)

    def test_invalid_authority_returns_redacted_unavailable_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            (state_dir / "sessions.json").write_text(
                '{"sessions":{"x":{"updated_at":NaN}}}', encoding="utf-8"
            )
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = main(
                    ["--state-dir", str(state_dir), "--user-id", "sensitive-user-id"]
                )

        self.assertEqual(exit_code, 3)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "CAPACITY_UNAVAILABLE")
        self.assertNotIn("sensitive-user-id", output.getvalue())
        self.assertNotIn(str(state_dir), output.getvalue())


if __name__ == "__main__":
    unittest.main()
