from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
EGRESS_DIR = ROOT / "apps" / "egress-gateway"
MODULE_PATH = EGRESS_DIR / "isolated_attempt.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "irlight_egress_isolated_attempt_target", MODULE_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("failed to load isolated attempt module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    sys.path.insert(0, str(EGRESS_DIR))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(EGRESS_DIR))
        sys.modules.pop(spec.name, None)
    return module


ISOLATED = _load_module()


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class _Connection:
    def __init__(self, clock: _Clock, messages: list[object]) -> None:
        self.clock = clock
        self.messages = list(messages)
        self.closed = False

    def poll(self, timeout: float = 0.0) -> bool:
        self.clock.advance(timeout)
        return bool(self.messages)

    def recv(self) -> object:
        return self.messages.pop(0)

    def close(self) -> None:
        self.closed = True


class _Event:
    def __init__(self, initial: bool = False) -> None:
        self.value = initial
        self.set_calls = 0

    def is_set(self) -> bool:
        return self.value

    def set(self) -> None:
        self.value = True
        self.set_calls += 1


class _Process:
    def __init__(self, alive: bool) -> None:
        self.alive = alive
        self.exitcode = None if alive else 0
        self.calls: list[tuple[str, float | None]] = []

    def is_alive(self) -> bool:
        self.calls.append(("is_alive", None))
        return self.alive

    def join(self, timeout: float | None = None) -> None:
        self.calls.append(("join", timeout))

    def terminate(self) -> None:
        self.calls.append(("terminate", None))
        self.alive = False
        self.exitcode = -15

    def kill(self) -> None:
        self.calls.append(("kill", None))
        self.alive = False
        self.exitcode = -9


class IsolatedAttemptContractTest(unittest.TestCase):
    def _result_message(
        self,
        *,
        kind: str = "result",
        reason: str = "TIMEOUT",
        connected: bool = True,
        rendered: int = 7,
        terminal: bool = False,
    ) -> dict[str, object]:
        return {
            "type": kind,
            "result": {
                "reason_code": reason,
                "connected_once": connected,
                "rendered_buffers": rendered,
                "terminal": terminal,
                "error_domain": None,
                "error_code": None,
            },
        }

    def test_canary_is_explicit_and_never_wraps_rtmp2sink(self) -> None:
        self.assertTrue(ISOLATED.legacy_isolation_enabled("1", "rtmpsink"))
        self.assertFalse(ISOLATED.legacy_isolation_enabled("0", "rtmpsink"))
        self.assertFalse(ISOLATED.legacy_isolation_enabled(None, "rtmpsink"))
        self.assertFalse(ISOLATED.legacy_isolation_enabled("1", "rtmp2sink"))

    def test_ipc_rejects_extra_secret_bearing_fields(self) -> None:
        with self.assertRaises(ISOLATED.InvalidChildMessage):
            ISOLATED.parse_child_message(
                {
                    "type": "progress",
                    "rendered_buffers": 1,
                    "destination_url": "rtmp://secret.invalid/live/key",
                }
            )

    def test_wrapper_does_not_retain_secret_values_in_child_config(self) -> None:
        with patch.dict(
            os.environ,
            {
                "EGRESS_URL_FILE": "/run/irlight/egress-secrets/egress_url",
                ISOLATED.TEARDOWN_TIMEOUT_ENV: "8",
                ISOLATED.TERMINATE_TIMEOUT_ENV: "2",
                ISOLATED.KILL_TIMEOUT_ENV: "2",
            },
            clear=False,
        ):
            wrapper = ISOLATED.IsolatedEgressAttempt(
                "rtsp://input-user:input-secret@media.invalid/live",
                "rtmp://publish.invalid/live/stream-secret",
                _Event(),
                connect_timeout_seconds=15,
                status_heartbeat_seconds=5,
                sink_factory="rtmpsink",
            )
        config_text = repr(wrapper.config)
        self.assertNotIn("input-secret", config_text)
        self.assertNotIn("stream-secret", config_text)
        self.assertEqual(
            wrapper.config.destination_file,
            "/run/irlight/egress-secrets/egress_url",
        )

    def test_valid_result_is_reaped_before_return(self) -> None:
        clock = _Clock()
        process = _Process(alive=False)
        connection = _Connection(clock, [self._result_message()])
        result = ISOLATED.supervise_attempt_child(
            process,
            connection,
            _Event(),
            _Event(),
            connect_timeout_seconds=15,
            teardown_timeout_seconds=8,
            terminate_timeout_seconds=2,
            kill_timeout_seconds=2,
            monotonic=clock,
        )
        self.assertEqual(result.reason_code, "TIMEOUT")
        self.assertTrue(result.connected_once)
        self.assertIn(("join", 0.5), process.calls)

    def test_teardown_timeout_fences_child_and_preserves_result(self) -> None:
        clock = _Clock()
        process = _Process(alive=True)
        connection = _Connection(
            clock,
            [self._result_message(kind="teardown", reason="TIMEOUT")],
        )
        result = ISOLATED.supervise_attempt_child(
            process,
            connection,
            _Event(),
            _Event(),
            connect_timeout_seconds=15,
            teardown_timeout_seconds=0.2,
            terminate_timeout_seconds=0.1,
            kill_timeout_seconds=0.1,
            poll_seconds=0.1,
            monotonic=clock,
        )
        self.assertEqual(result.reason_code, "TIMEOUT")
        self.assertTrue(result.connected_once)
        self.assertIn(("terminate", None), process.calls)
        self.assertFalse(process.is_alive())

    def test_child_exit_after_teardown_preserves_validated_terminal_result(self) -> None:
        clock = _Clock()
        process = _Process(alive=False)
        connection = _Connection(
            clock,
            [
                self._result_message(
                    kind="teardown",
                    reason="AUTH_FAILED",
                    connected=False,
                    rendered=0,
                    terminal=True,
                )
            ],
        )
        result = ISOLATED.supervise_attempt_child(
            process,
            connection,
            _Event(),
            _Event(),
            connect_timeout_seconds=15,
            teardown_timeout_seconds=8,
            terminate_timeout_seconds=2,
            kill_timeout_seconds=2,
            monotonic=clock,
        )
        self.assertEqual(result.reason_code, "AUTH_FAILED")
        self.assertTrue(result.terminal)
        self.assertIn(("join", 0.0), process.calls)

    def test_startup_timeout_fences_preconnect_child(self) -> None:
        clock = _Clock()
        process = _Process(alive=True)
        connection = _Connection(clock, [])
        result = ISOLATED.supervise_attempt_child(
            process,
            connection,
            _Event(),
            _Event(),
            connect_timeout_seconds=0.2,
            teardown_timeout_seconds=0.2,
            terminate_timeout_seconds=0.1,
            kill_timeout_seconds=0.1,
            poll_seconds=0.1,
            monotonic=clock,
        )
        self.assertEqual(result.reason_code, "TIMEOUT")
        self.assertFalse(result.terminal)
        self.assertIn(("terminate", None), process.calls)

    def test_malformed_ipc_fences_child_and_fails_closed(self) -> None:
        clock = _Clock()
        process = _Process(alive=True)
        connection = _Connection(
            clock,
            [
                {
                    "type": "result",
                    "result": {
                        "reason_code": "TIMEOUT",
                        "connected_once": False,
                        "rendered_buffers": 0,
                        "terminal": False,
                        "destination_url": "rtmp://secret.invalid/live/key",
                    },
                }
            ],
        )
        result = ISOLATED.supervise_attempt_child(
            process,
            connection,
            _Event(),
            _Event(),
            connect_timeout_seconds=15,
            teardown_timeout_seconds=8,
            terminate_timeout_seconds=0.1,
            kill_timeout_seconds=0.1,
            monotonic=clock,
        )
        self.assertEqual(result.reason_code, "LOCAL_PIPELINE_FAILED")
        self.assertTrue(result.terminal)
        self.assertIn(("terminate", None), process.calls)

    def test_user_stop_deadline_is_not_starved_by_progress_messages(self) -> None:
        clock = _Clock()
        process = _Process(alive=True)
        progress = {"type": "progress", "rendered_buffers": 3}
        connection = _Connection(clock, [progress.copy() for _ in range(10)])
        child_stop = _Event()
        result = ISOLATED.supervise_attempt_child(
            process,
            connection,
            child_stop,
            _Event(initial=True),
            connect_timeout_seconds=15,
            teardown_timeout_seconds=0.25,
            terminate_timeout_seconds=0.1,
            kill_timeout_seconds=0.1,
            poll_seconds=0.1,
            monotonic=clock,
        )
        self.assertEqual(result.reason_code, "STOPPED")
        self.assertEqual(child_stop.set_calls, 1)
        self.assertIn(("terminate", None), process.calls)

    def test_invalid_isolation_timeouts_fail_closed_without_echoing_value(self) -> None:
        for value in ("-1", "nan", "inf", "-inf", "not-a-number"):
            with self.subTest(value=value), patch.dict(
                os.environ,
                {ISOLATED.TEARDOWN_TIMEOUT_ENV: value},
                clear=False,
            ):
                with self.assertRaises(ValueError) as raised:
                    ISOLATED.IsolatedEgressAttempt(
                        "rtsp://media.invalid/live",
                        "rtmp://publish.invalid/live/key",
                        _Event(),
                        connect_timeout_seconds=15,
                        status_heartbeat_seconds=5,
                        sink_factory="rtmpsink",
                    )
                self.assertNotIn(value, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
