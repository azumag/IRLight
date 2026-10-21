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


class _HeartbeatConnection:
    """A healthy child reporting only on the real nominal 250 ms sink polls.

    CONNECTED resets the producer's heartbeat timestamp at t=0. A heartbeat
    of 0 disables progress; otherwise a poll reports only once it is due.
    This fixed-callback fake does not model scheduler or IPC jitter.
    """

    def __init__(self, clock: _Clock, cadence: float) -> None:
        self.clock = clock
        self.cadence = cadence
        self.pending: list[object] = [{"type": "connected", "rendered_buffers": 1}]
        self.next_sink_poll = 0.25
        self.last_report = 0.0
        self.progress_sent = 0

    def poll(self, timeout: float = 0.0) -> bool:
        if self.pending:
            return True
        deadline = self.clock.value + timeout
        while self.next_sink_poll <= deadline:
            self.clock.value = self.next_sink_poll
            self.next_sink_poll += 0.25
            if self.cadence > 0 and self.clock.value - self.last_report >= self.cadence:
                self.last_report = self.clock.value
                self.progress_sent += 1
                self.pending.append(
                    {"type": "progress", "rendered_buffers": self.progress_sent + 1}
                )
                return True
        self.clock.value = deadline
        return False

    def recv(self) -> object:
        return self.pending.pop(0)

    def close(self) -> None:
        pass


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
    def __init__(self, alive: bool, dies_after_polls: int | None = None) -> None:
        self.alive = alive
        self.exitcode = None if alive else 0
        self.calls: list[tuple[str, float | None]] = []
        self.dies_after_polls = dies_after_polls
        self.polls = 0

    def is_alive(self) -> bool:
        self.calls.append(("is_alive", None))
        self.polls += 1
        if self.dies_after_polls is not None and self.polls >= self.dies_after_polls:
            self.alive = False
            self.exitcode = 0
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
            liveness_timeout_seconds=20,
            status_heartbeat_seconds=5,
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
            liveness_timeout_seconds=20,
            status_heartbeat_seconds=5,
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
            liveness_timeout_seconds=20,
            status_heartbeat_seconds=5,
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
            liveness_timeout_seconds=20,
            status_heartbeat_seconds=5,
            teardown_timeout_seconds=0.2,
            terminate_timeout_seconds=0.1,
            kill_timeout_seconds=0.1,
            poll_seconds=0.1,
            monotonic=clock,
        )
        self.assertEqual(result.reason_code, "TIMEOUT")
        self.assertFalse(result.terminal)
        self.assertIn(("terminate", None), process.calls)

    def test_healthy_child_is_never_fenced_by_the_liveness_window(self) -> None:
        # The child's only post-connect liveness evidence is the status
        # heartbeat, so a window that cannot be refreshed inside the cadence
        # would fence a healthy attempt and drive the RECONNECTING loop this
        # bound exists to shorten. Every permitted configuration must survive.
        for cadence, liveness in (
            (5.0, 20.0),   # production default heartbeat and window
            (5.0, 5.0),    # window exactly equal to the cadence
            (30.0, 20.0),  # cadence slower than the default window
            (0.0, 20.0),   # heartbeat disabled: no evidence channel at all
            (0.1, 0.15),   # next possible progress is at 0.25, not 0.1
            (0.1, 0.25),   # equal to the nominal poll-rounded report interval
            (0.3, 0.4),    # next possible progress is at 0.5, not 0.3
            (0.3, 0.5),    # equal to the nominal poll-rounded report interval
            (0.1, 0.3),    # short window wider than the 0.25 report interval
            (0.3, 0.6),    # short window wider than the 0.5 report interval
        ):
            with self.subTest(cadence=cadence, liveness=liveness):
                clock = _Clock()
                process = _Process(alive=True, dies_after_polls=400)
                connection = _HeartbeatConnection(clock, cadence)
                ISOLATED.supervise_attempt_child(
                    process,
                    connection,
                    _Event(),
                    _Event(),
                    connect_timeout_seconds=15,
                    liveness_timeout_seconds=liveness,
                    status_heartbeat_seconds=cadence,
                    teardown_timeout_seconds=8,
                    terminate_timeout_seconds=2,
                    kill_timeout_seconds=2,
                    poll_seconds=0.1,
                    monotonic=clock,
                )
                self.assertNotIn(("terminate", None), process.calls)
                self.assertNotIn(("kill", None), process.calls)

    def test_post_connect_ipc_silence_fences_child_and_publishes_reconnecting(
        self,
    ) -> None:
        # The #545 failure mode: the child survives CONNECTED but wedges in a
        # blocking sink call and never emits teardown or a result. Silence must
        # become a bounded, retryable timeout instead of a stale CONNECTED.
        clock = _Clock()
        process = _Process(alive=True)
        connection = _Connection(clock, [{"type": "connected", "rendered_buffers": 5}])
        result = ISOLATED.supervise_attempt_child(
            process,
            connection,
            _Event(),
            _Event(),
            connect_timeout_seconds=15,
            liveness_timeout_seconds=0.3,
            status_heartbeat_seconds=0.1,
            teardown_timeout_seconds=8,
            terminate_timeout_seconds=0.1,
            kill_timeout_seconds=0.1,
            poll_seconds=0.1,
            monotonic=clock,
        )
        self.assertEqual(result.reason_code, "TIMEOUT")
        self.assertTrue(result.connected_once)
        self.assertFalse(result.terminal)
        self.assertEqual(result.rendered_buffers, 5)
        self.assertIn(("terminate", None), process.calls)
        self.assertFalse(process.is_alive())

    def test_post_connect_progress_refreshes_liveness_deadline(self) -> None:
        # A healthy child reports on every heartbeat, so the liveness window
        # must never fire while progress keeps arriving even though the elapsed
        # time far exceeds the window itself.
        clock = _Clock()
        process = _Process(alive=True)
        messages: list[object] = [{"type": "connected", "rendered_buffers": 1}]
        messages += [
            {"type": "progress", "rendered_buffers": index} for index in range(2, 40)
        ]
        connection = _Connection(clock, messages)
        result = ISOLATED.supervise_attempt_child(
            process,
            connection,
            _Event(),
            _Event(),
            connect_timeout_seconds=15,
            liveness_timeout_seconds=0.5,
            status_heartbeat_seconds=0.1,
            teardown_timeout_seconds=8,
            terminate_timeout_seconds=0.1,
            kill_timeout_seconds=0.1,
            poll_seconds=0.1,
            monotonic=clock,
        )
        self.assertEqual(result.rendered_buffers, 39)
        self.assertEqual(result.reason_code, "TIMEOUT")
        # 39 messages span 3.9s against a 0.5s window, so the child was fenced
        # only after it actually went silent, within one poll of that deadline.
        self.assertGreater(clock.value, 3.9)
        self.assertLess(clock.value, 3.9 + 0.5 + 2 * 0.1)
        self.assertIn(("terminate", None), process.calls)

    def test_zero_liveness_timeout_leaves_post_connect_silence_unfenced(self) -> None:
        # 0 keeps the historical unbounded post-connect behavior, so a silent
        # child must not be fenced by the liveness deadline at all. The child
        # exits on its own instead.
        clock = _Clock()
        process = _Process(alive=True, dies_after_polls=4)
        connection = _Connection(clock, [{"type": "connected", "rendered_buffers": 2}])
        result = ISOLATED.supervise_attempt_child(
            process,
            connection,
            _Event(),
            _Event(),
            connect_timeout_seconds=15,
            liveness_timeout_seconds=0,
            status_heartbeat_seconds=5,
            teardown_timeout_seconds=8,
            terminate_timeout_seconds=0.1,
            kill_timeout_seconds=0.1,
            poll_seconds=0.1,
            monotonic=clock,
        )
        self.assertEqual(result.reason_code, "LOCAL_PIPELINE_FAILED")
        self.assertTrue(result.connected_once)
        self.assertNotIn(("terminate", None), process.calls)
        self.assertNotIn(("kill", None), process.calls)

    def test_invalid_liveness_timeout_is_rejected_without_echoing_value(self) -> None:
        for value in ("-1", "nan", "inf", "-inf", "not-a-number"):
            with self.subTest(value=value), patch.dict(
                os.environ,
                {ISOLATED.LIVENESS_TIMEOUT_ENV: value},
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

    def test_repeated_teardown_messages_cannot_postpone_fencing(self) -> None:
        # The child-side monitor emits one marker, so a second valid teardown
        # message can only be an attempt to keep the deadline moving.
        clock = _Clock()
        process = _Process(alive=True)
        teardown = self._result_message(kind="teardown", reason="TIMEOUT")
        connection = _Connection(clock, [teardown.copy() for _ in range(10)])
        result = ISOLATED.supervise_attempt_child(
            process,
            connection,
            _Event(),
            _Event(),
            connect_timeout_seconds=15,
            liveness_timeout_seconds=20,
            status_heartbeat_seconds=5,
            teardown_timeout_seconds=0.25,
            terminate_timeout_seconds=0.1,
            kill_timeout_seconds=0.1,
            poll_seconds=0.1,
            monotonic=clock,
        )
        self.assertEqual(result.reason_code, "TIMEOUT")
        # The first deadline was armed at 0.1s and fires at 0.35s; the child is
        # fenced long before the tenth message could have extended it.
        self.assertLess(clock.value, 0.1 * 10)
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
            liveness_timeout_seconds=20,
            status_heartbeat_seconds=5,
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
            liveness_timeout_seconds=20,
            status_heartbeat_seconds=5,
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
