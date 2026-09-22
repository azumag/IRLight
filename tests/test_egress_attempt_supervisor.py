from __future__ import annotations

import importlib.util
import math
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "apps" / "egress-gateway" / "attempt_supervisor.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "irlight_egress_attempt_supervisor_target", MODULE_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("failed to load attempt supervisor module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


SUPERVISOR = _load_module()


class _FakeProcess:
    def __init__(self, exits_on: str | None) -> None:
        self.exits_on = exits_on
        self.alive = True
        self.exitcode = None
        self.calls: list[tuple[str, float | None]] = []

    def join(self, timeout: float | None = None) -> None:
        self.calls.append(("join", timeout))
        if self.exits_on == "join" and len([c for c in self.calls if c[0] == "join"]) == 1:
            self.alive = False
            self.exitcode = 0
        elif self.exits_on == "terminate" and any(c[0] == "terminate" for c in self.calls):
            self.alive = False
            self.exitcode = -15
        elif self.exits_on == "kill" and any(c[0] == "kill" for c in self.calls):
            self.alive = False
            self.exitcode = -9

    def is_alive(self) -> bool:
        self.calls.append(("is_alive", None))
        return self.alive

    def terminate(self) -> None:
        self.calls.append(("terminate", None))
        if self.exits_on == "terminate-race":
            self.alive = False
            self.exitcode = 0
            raise ProcessLookupError("child already exited")
        if self.exits_on == "terminate-permission":
            raise PermissionError("signal denied")

    def kill(self) -> None:
        self.calls.append(("kill", None))
        if self.exits_on == "kill-race":
            self.alive = False
            self.exitcode = -15
            raise ProcessLookupError("child exited after terminate")


class AttemptResultContractTest(unittest.TestCase):
    def test_accepts_secret_free_result(self) -> None:
        result = SUPERVISOR.parse_child_attempt_result(
            {
                "reason_code": "TIMEOUT",
                "connected_once": True,
                "rendered_buffers": 42,
                "terminal": False,
                "error_domain": "gst-resource-error-quark",
                "error_code": 7,
            }
        )
        self.assertEqual(result.reason_code, "TIMEOUT")
        self.assertEqual(result.rendered_buffers, 42)

    def test_rejects_extra_secret_bearing_fields(self) -> None:
        with self.assertRaises(SUPERVISOR.InvalidAttemptResult):
            SUPERVISOR.parse_child_attempt_result(
                {
                    "reason_code": "TIMEOUT",
                    "connected_once": True,
                    "rendered_buffers": 1,
                    "terminal": False,
                    "destination_url": "rtmp://secret.example/live/key",
                }
            )

    def test_rejects_bool_counter_and_unbounded_text(self) -> None:
        bad_payloads = [
            {
                "reason_code": "TIMEOUT",
                "connected_once": True,
                "rendered_buffers": True,
                "terminal": False,
            },
            {
                "reason_code": "timeout contains details",
                "connected_once": True,
                "rendered_buffers": 1,
                "terminal": False,
            },
            {
                "reason_code": "TIMEOUT",
                "connected_once": True,
                "rendered_buffers": 1,
                "terminal": False,
                "error_domain": "rtmp://secret.example/live/key",
            },
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(SUPERVISOR.InvalidAttemptResult):
                    SUPERVISOR.parse_child_attempt_result(payload)


class ChildReapingContractTest(unittest.TestCase):
    def test_natural_exit_does_not_signal_child(self) -> None:
        process = _FakeProcess("join")
        result = SUPERVISOR.reap_child(
            process,
            natural_timeout_seconds=2,
            terminate_timeout_seconds=1,
            kill_timeout_seconds=1,
        )
        self.assertEqual(result.disposition, "exited")
        self.assertNotIn(("terminate", None), process.calls)
        self.assertNotIn(("kill", None), process.calls)

    def test_hung_child_is_terminated_and_reaped_before_return(self) -> None:
        process = _FakeProcess("terminate")
        result = SUPERVISOR.reap_child(
            process,
            natural_timeout_seconds=2,
            terminate_timeout_seconds=1,
            kill_timeout_seconds=1,
        )
        self.assertEqual(result.disposition, "terminated")
        self.assertFalse(process.alive)
        self.assertNotIn(("kill", None), process.calls)

    def test_terminate_resistant_child_is_killed_and_reaped(self) -> None:
        process = _FakeProcess("kill")
        result = SUPERVISOR.reap_child(
            process,
            natural_timeout_seconds=2,
            terminate_timeout_seconds=1,
            kill_timeout_seconds=1,
        )
        self.assertEqual(result.disposition, "killed")
        self.assertFalse(process.alive)
        self.assertIn(("terminate", None), process.calls)
        self.assertIn(("kill", None), process.calls)

    def test_exit_race_before_terminate_is_reaped_as_natural_exit(self) -> None:
        process = _FakeProcess("terminate-race")
        result = SUPERVISOR.reap_child(
            process,
            natural_timeout_seconds=2,
            terminate_timeout_seconds=1,
            kill_timeout_seconds=1,
        )
        self.assertEqual(result.disposition, "exited")
        self.assertFalse(process.alive)
        self.assertNotIn(("kill", None), process.calls)

    def test_exit_race_before_kill_preserves_terminate_disposition(self) -> None:
        process = _FakeProcess("kill-race")
        result = SUPERVISOR.reap_child(
            process,
            natural_timeout_seconds=2,
            terminate_timeout_seconds=1,
            kill_timeout_seconds=1,
        )
        self.assertEqual(result.disposition, "terminated")
        self.assertFalse(process.alive)
        self.assertIn(("terminate", None), process.calls)
        self.assertIn(("kill", None), process.calls)

    def test_non_esrch_signal_errors_are_not_swallowed(self) -> None:
        process = _FakeProcess("terminate-permission")
        with self.assertRaises(PermissionError):
            SUPERVISOR.reap_child(
                process,
                natural_timeout_seconds=2,
                terminate_timeout_seconds=1,
                kill_timeout_seconds=1,
            )

    def test_unreapable_child_fails_closed(self) -> None:
        process = _FakeProcess(None)
        with self.assertRaises(SUPERVISOR.AttemptSupervisorError):
            SUPERVISOR.reap_child(
                process,
                natural_timeout_seconds=2,
                terminate_timeout_seconds=1,
                kill_timeout_seconds=1,
            )
        self.assertTrue(process.alive)

    def test_invalid_timeouts_are_rejected_before_process_calls(self) -> None:
        invalid_values = [-1, math.nan, math.inf, -math.inf, True, "not-a-number"]
        for value in invalid_values:
            with self.subTest(value=value):
                process = _FakeProcess("join")
                with self.assertRaises(ValueError):
                    SUPERVISOR.reap_child(
                        process,
                        natural_timeout_seconds=value,
                        terminate_timeout_seconds=1,
                        kill_timeout_seconds=1,
                    )
                self.assertEqual(process.calls, [])


if __name__ == "__main__":
    unittest.main()
