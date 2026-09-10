from __future__ import annotations

import ast
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))
os.environ.setdefault(
    "STATE_DIR", tempfile.mkdtemp(prefix="irlight-reaper-cli-auth-gc-")
)

import reaper_cli  # noqa: E402
from auth_session_gc import PruneResult  # noqa: E402
from auth_store import AuthStateError  # noqa: E402


class ReaperCliAuthSessionGcTests(unittest.TestCase):
    @staticmethod
    def _reaper_mock(result: dict[str, object]) -> Mock:
        instance = Mock()
        instance.run.return_value = dict(result)
        return instance

    def test_main_runs_auth_gc_and_preserves_flat_reaper_output(self) -> None:
        reaper = self._reaper_mock({"deadline_stops": 1})
        stream = io.StringIO()
        with (
            patch("reaper_cli.default_store", return_value=object()),
            patch("reaper_cli.default_provider", return_value=object()),
            patch("reaper_cli.Reaper", return_value=reaper),
            patch(
                "reaper_cli.prune_expired_sessions",
                return_value=PruneResult(
                    scanned=5,
                    deleted=2,
                    expired_remaining=1,
                    dry_run=False,
                ),
            ) as prune,
            redirect_stdout(stream),
        ):
            rc = reaper_cli.main(["--auth-session-gc-max-delete", "25"])

        self.assertEqual(rc, 0)
        reaper.run.assert_called_once()
        prune.assert_called_once_with(max_deletions=25)
        result = ast.literal_eval(stream.getvalue().strip().splitlines()[-1])
        self.assertEqual(result["deadline_stops"], 1)
        self.assertEqual(result["auth_session_gc_status"], "ok")
        self.assertEqual(result["auth_session_gc_scanned"], 5)
        self.assertEqual(result["auth_session_gc_deleted"], 2)
        self.assertEqual(result["auth_session_gc_expired_remaining"], 1)

    def test_auth_gc_failure_is_reported_after_session_reaper(self) -> None:
        reaper = self._reaper_mock({"orphan_cleanup": 3})
        stream = io.StringIO()
        with (
            patch("reaper_cli.default_store", return_value=object()),
            patch("reaper_cli.default_provider", return_value=object()),
            patch("reaper_cli.Reaper", return_value=reaper),
            patch(
                "reaper_cli.prune_expired_sessions",
                side_effect=AuthStateError(
                    "secret internal /state/auth_sessions.json"
                ),
            ),
            patch.object(reaper_cli.LOG, "error") as log_error,
            redirect_stdout(stream),
        ):
            rc = reaper_cli.main([])

        self.assertEqual(rc, 1)
        reaper.run.assert_called_once()
        result = ast.literal_eval(stream.getvalue().strip().splitlines()[-1])
        self.assertEqual(result["orphan_cleanup"], 3)
        self.assertEqual(result["auth_session_gc_status"], "failed")
        self.assertEqual(result["auth_session_gc_reason"], "AUTH_SESSION_GC_FAILED")
        self.assertNotIn("/state/", stream.getvalue())
        self.assertNotIn("auth_sessions.json", stream.getvalue())
        log_text = " ".join(
            str(arg)
            for call in log_error.call_args_list
            for arg in call.args
        )
        self.assertNotIn("/state/", log_text)
        self.assertNotIn("auth_sessions.json", log_text)

    def test_invalid_gc_limit_does_not_skip_session_reaper(self) -> None:
        reaper = self._reaper_mock({"timeout_failures": 0})
        stream = io.StringIO()
        with (
            patch("reaper_cli.default_store", return_value=object()),
            patch("reaper_cli.default_provider", return_value=object()),
            patch("reaper_cli.Reaper", return_value=reaper),
            patch.object(reaper_cli.LOG, "error"),
            redirect_stdout(stream),
        ):
            rc = reaper_cli.main(["--auth-session-gc-max-delete", "0"])

        self.assertEqual(rc, 1)
        reaper.run.assert_called_once()
        result = ast.literal_eval(stream.getvalue().strip().splitlines()[-1])
        self.assertEqual(result["auth_session_gc_status"], "failed")
        self.assertEqual(result["auth_session_gc_reason"], "AUTH_SESSION_GC_FAILED")


if __name__ == "__main__":
    unittest.main()
