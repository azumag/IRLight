from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "node-agent"))

from egress_failure_inspect_cli import diagnose_egress, main as inspect_main  # noqa: E402


class EgressFailureInspectionTest(unittest.TestCase):
    def _write(self, payload: dict[str, object]) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        tmp = tempfile.TemporaryDirectory(prefix="irlight-egress-inspect-")
        path = Path(tmp.name) / "egress.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return tmp, path

    def test_connected_is_ok(self) -> None:
        tmp, path = self._write(
            {
                "status": "CONNECTED",
                "connected": True,
                "attempt": 1,
                "destination_scheme": "rtmps",
                "destination_host": "live.example",
                "observed_at": 100.0,
            }
        )
        try:
            payload, exit_code = diagnose_egress(path, now=105.0, max_age_seconds=30.0)
        finally:
            tmp.cleanup()
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["action_code"], "NONE")
        self.assertEqual(payload["destination_host"], "live.example")

    def test_reconnecting_dns_failure_is_retrying(self) -> None:
        tmp, path = self._write(
            {
                "status": "RECONNECTING",
                "connected": False,
                "attempt": 3,
                "reason_code": "DNS_FAILED",
                "next_retry_at": 110.0,
                "observed_at": 100.0,
            }
        )
        try:
            payload, exit_code = diagnose_egress(path, now=105.0, max_age_seconds=30.0)
        finally:
            tmp.cleanup()
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "RETRYING")
        self.assertEqual(payload["action_code"], "CHECK_DESTINATION_DNS")
        self.assertEqual(payload["next_retry_in_seconds"], 5.0)

    def test_auth_failure_redacts_underlying_secret_fields(self) -> None:
        tmp, path = self._write(
            {
                "status": "AUTH_FAILED",
                "connected": False,
                "attempt": 1,
                "reason_code": "AUTH_FAILED",
                "destination_scheme": "rtmps",
                "destination_host": "live.example",
                "observed_at": 100.0,
                "credentialed_url": "rtmps://live.example/app/AUDIT_DUMMY_SECRET",
                "stream_key": "AUDIT_DUMMY_SECRET",
            }
        )
        try:
            payload, exit_code = diagnose_egress(path, now=1000.0, max_age_seconds=30.0)
        finally:
            tmp.cleanup()
        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "TERMINAL")
        self.assertEqual(payload["action_code"], "CHECK_DESTINATION_CREDENTIAL")
        self.assertNotIn("AUDIT_DUMMY_SECRET", json.dumps(payload))

    def test_publish_conflict_is_not_treated_as_retryable(self) -> None:
        tmp, path = self._write(
            {
                "status": "AUTH_FAILED",
                "connected": False,
                "reason_code": "PUBLISH_CONFLICT",
                "observed_at": 100.0,
            }
        )
        try:
            payload, exit_code = diagnose_egress(path, now=101.0, max_age_seconds=30.0)
        finally:
            tmp.cleanup()
        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["action_code"], "CHECK_PUBLISH_CONFLICT")

    def test_local_pipeline_failure_is_terminal(self) -> None:
        tmp, path = self._write(
            {
                "status": "FAILED",
                "connected": False,
                "reason_code": "LOCAL_PIPELINE_FAILED",
                "observed_at": 100.0,
            }
        )
        try:
            payload, exit_code = diagnose_egress(path, now=101.0, max_age_seconds=30.0)
        finally:
            tmp.cleanup()
        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["action_code"], "CHECK_LOCAL_MEDIA_PATH")

    def test_missing_and_stale_status_are_unavailable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-egress-inspect-") as tmp:
            missing, missing_code = diagnose_egress(
                Path(tmp) / "missing.json", now=200.0, max_age_seconds=30.0
            )
            path = Path(tmp) / "egress.json"
            path.write_text(
                json.dumps({"status": "STARTING", "observed_at": 100.0}),
                encoding="utf-8",
            )
            stale, stale_code = diagnose_egress(path, now=200.0, max_age_seconds=30.0)
        self.assertEqual(missing_code, 3)
        self.assertEqual(missing["reason_code"], "STATUS_UNAVAILABLE")
        self.assertEqual(stale_code, 3)
        self.assertEqual(stale["reason_code"], "STATUS_STALE")
        self.assertEqual(stale["action_code"], "CHECK_EGRESS_STATUS_SOURCE")

    def test_unknown_reason_is_not_echoed(self) -> None:
        tmp, path = self._write(
            {
                "status": "FAILED",
                "reason_code": "secret=AUDIT_DUMMY_SECRET",
                "observed_at": 100.0,
            }
        )
        try:
            payload, exit_code = diagnose_egress(path, now=101.0, max_age_seconds=30.0)
        finally:
            tmp.cleanup()
        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["reason_code"], "UNCLASSIFIED")
        self.assertNotIn("AUDIT_DUMMY_SECRET", json.dumps(payload))

    def test_stopped_requires_desired_state_confirmation(self) -> None:
        tmp, path = self._write({"status": "STOPPED", "observed_at": 100.0})
        try:
            payload, exit_code = diagnose_egress(path, now=500.0, max_age_seconds=30.0)
        finally:
            tmp.cleanup()
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "ATTENTION")
        self.assertEqual(payload["action_code"], "CONFIRM_SESSION_DESIRED_STATE")


class EgressFailureInspectCliTest(unittest.TestCase):
    def test_cli_prints_json_and_returns_failure_code(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-egress-inspect-") as tmp:
            path = Path(tmp) / "egress.json"
            path.write_text(
                json.dumps(
                    {
                        "status": "FAILED",
                        "reason_code": "SECRET_UNAVAILABLE",
                        "observed_at": 100.0,
                    }
                ),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = inspect_main(
                    ["--status-file", str(path), "--max-age-seconds", "30"]
                )
        self.assertEqual(exit_code, 2)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["action_code"], "CHECK_SECRET_DELIVERY")

    def test_invalid_max_age_is_argument_error(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                inspect_main(["--max-age-seconds", "nan"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("finite positive number", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
