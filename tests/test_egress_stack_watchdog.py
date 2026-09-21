from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EGRESS_DIR = ROOT / "apps" / "egress-gateway"
sys.path.insert(0, str(EGRESS_DIR))

from stack_watchdog import ConnectedStatusStackWatchdog  # noqa: E402


class EgressStackWatchdogTest(unittest.TestCase):
    def _status_path(self, directory: str, payload: dict[str, object]) -> Path:
        path = Path(directory, "egress.json")
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_zero_heartbeat_disables_watchdog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._status_path(
                tmp,
                {
                    "status": "CONNECTED",
                    "connected": True,
                    "observed_at": 101.0,
                },
            )
            output = io.StringIO()
            watchdog = ConnectedStatusStackWatchdog(
                path,
                heartbeat_seconds=0.0,
                started_at=100.0,
                now=lambda: 200.0,
                dump_traceback=lambda **_kwargs: self.fail("must not dump"),
                output=output,
            )
            self.assertFalse(watchdog.enabled)
            self.assertFalse(watchdog.inspect_once())
            self.assertEqual(output.getvalue(), "")

    def test_previous_process_connected_record_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._status_path(
                tmp,
                {
                    "status": "CONNECTED",
                    "connected": True,
                    "observed_at": 99.0,
                },
            )
            output = io.StringIO()
            watchdog = ConnectedStatusStackWatchdog(
                path,
                heartbeat_seconds=5.0,
                started_at=100.0,
                now=lambda: 200.0,
                dump_traceback=lambda **_kwargs: self.fail("must not dump"),
                output=output,
            )
            self.assertFalse(watchdog.inspect_once())
            self.assertEqual(output.getvalue(), "")

    def test_current_stale_connected_status_dumps_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._status_path(
                tmp,
                {
                    "status": "CONNECTED",
                    "connected": True,
                    "observed_at": 101.0,
                    "destination_host": "secret.example.invalid",
                    "stream_key": "AUDIT_DUMMY_SECRET",
                },
            )
            output = io.StringIO()
            calls: list[dict[str, object]] = []

            def dump_traceback(**kwargs: object) -> None:
                calls.append(kwargs)

            watchdog = ConnectedStatusStackWatchdog(
                path,
                heartbeat_seconds=5.0,
                started_at=100.0,
                now=lambda: 117.0,
                dump_traceback=dump_traceback,
                output=output,
            )
            self.assertTrue(watchdog.inspect_once())
            self.assertFalse(watchdog.inspect_once())
            self.assertEqual(len(calls), 1)
            self.assertIs(calls[0]["file"], output)
            self.assertIs(calls[0]["all_threads"], True)
            self.assertEqual(
                output.getvalue(),
                "IRLIGHT_EGRESS_STALE_STATUS_STACK status=CONNECTED age_bucket=stale\n",
            )
            self.assertNotIn("AUDIT_DUMMY_SECRET", output.getvalue())
            self.assertNotIn("secret.example.invalid", output.getvalue())

    def test_fresh_status_does_not_dump(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._status_path(
                tmp,
                {
                    "status": "CONNECTED",
                    "connected": True,
                    "observed_at": 110.0,
                },
            )
            watchdog = ConnectedStatusStackWatchdog(
                path,
                heartbeat_seconds=5.0,
                started_at=100.0,
                now=lambda: 120.0,
                dump_traceback=lambda **_kwargs: self.fail("must not dump"),
                output=io.StringIO(),
            )
            self.assertFalse(watchdog.inspect_once())

    def test_refreshed_status_can_report_a_later_stall(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._status_path(
                tmp,
                {
                    "status": "CONNECTED",
                    "connected": True,
                    "observed_at": 101.0,
                },
            )
            current = [117.0]
            calls: list[dict[str, object]] = []
            watchdog = ConnectedStatusStackWatchdog(
                path,
                heartbeat_seconds=5.0,
                started_at=100.0,
                now=lambda: current[0],
                dump_traceback=lambda **kwargs: calls.append(kwargs),
                output=io.StringIO(),
            )
            self.assertTrue(watchdog.inspect_once())
            path.write_text(
                json.dumps(
                    {
                        "status": "CONNECTED",
                        "connected": True,
                        "observed_at": 120.0,
                    }
                ),
                encoding="utf-8",
            )
            current[0] = 136.0
            self.assertTrue(watchdog.inspect_once())
            self.assertEqual(len(calls), 2)

    def test_malformed_or_nonfinite_status_fails_closed(self) -> None:
        malformed_payloads = (
            "{broken",
            '{"status":"CONNECTED","connected":true,"observed_at":NaN}',
            '{"status":"CONNECTED","connected":true,"observed_at":Infinity}',
        )
        for payload in malformed_payloads:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp, "egress.json")
                path.write_text(payload, encoding="utf-8")
                output = io.StringIO()
                watchdog = ConnectedStatusStackWatchdog(
                    path,
                    heartbeat_seconds=5.0,
                    started_at=100.0,
                    now=lambda: 200.0,
                    dump_traceback=lambda **_kwargs: self.fail("must not dump"),
                    output=output,
                )
                self.assertFalse(watchdog.inspect_once())
                self.assertEqual(output.getvalue(), "")

    def test_production_entrypoint_starts_watchdog_before_gateway(self) -> None:
        source = (EGRESS_DIR / "egress_entrypoint.py").read_text(encoding="utf-8")
        self.assertIn("from stack_watchdog import ConnectedStatusStackWatchdog", source)
        main_body = source.split("def main() -> int:", 1)[1]
        self.assertLess(main_body.index("_start_stack_watchdog()"), main_body.index("return egress.main()"))

    def test_container_packages_watchdog(self) -> None:
        dockerfile = (EGRESS_DIR / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("stack_watchdog.py", dockerfile)


if __name__ == "__main__":
    unittest.main()
