from __future__ import annotations

import io
import queue
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from destination_probe import (  # noqa: E402
    DestinationProbeError,
    ProbeConfig,
    SRT_CONNECTED_MARKERS,
    SRT_STDERR_EVENT_CONNECTED,
    SRT_STDERR_EVENT_OVERFLOW,
    SRT_STDERR_MAX_BYTES,
    SRT_STDERR_READ_CHUNK_BYTES,
    _read_srt_stderr_event,
    probe_destination,
)


class _FakeProcess:
    def __init__(self, stderr_data: bytes) -> None:
        self.stderr = io.BytesIO(stderr_data)
        self.stdin = io.BytesIO()
        self._returncode: int | None = None
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self._returncode

    def terminate(self) -> None:
        self.terminated = True
        self._returncode = -15

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        if self._returncode is None:
            self._returncode = 0
        return self._returncode

    def kill(self) -> None:
        self.killed = True
        self._returncode = -9


class SrtStderrBudgetTest(unittest.TestCase):
    def test_connected_marker_can_cross_read_chunk_boundary(self) -> None:
        marker = SRT_CONNECTED_MARKERS[1]
        prefix = b"x" * (SRT_STDERR_READ_CHUNK_BYTES - 5)
        stream = io.BytesIO(prefix + marker + b"\n")
        events: queue.Queue[str] = queue.Queue(maxsize=1)

        _read_srt_stderr_event(stream, events)

        self.assertEqual(events.get_nowait(), SRT_STDERR_EVENT_CONNECTED)

    def test_long_no_newline_output_is_bounded(self) -> None:
        stream = io.BytesIO(b"x" * (SRT_STDERR_MAX_BYTES + 4096))
        events: queue.Queue[str] = queue.Queue(maxsize=1)

        _read_srt_stderr_event(stream, events)

        self.assertEqual(events.get_nowait(), SRT_STDERR_EVENT_OVERFLOW)
        self.assertLessEqual(stream.tell(), SRT_STDERR_MAX_BYTES + 1)

    @patch("destination_probe.subprocess.Popen")
    def test_probe_fails_closed_and_terminates_on_stderr_flood(self, popen) -> None:
        process = _FakeProcess(b"x" * (SRT_STDERR_MAX_BYTES + 1))
        popen.return_value = process

        with self.assertRaisesRegex(DestinationProbeError, "output exceeded safety limit"):
            probe_destination(
                "srt://127.0.0.1:8890?streamid=publish:probe",
                ProbeConfig(
                    timeout_seconds=1.0,
                    allow_private_targets=True,
                    srt_binary="srt-live-transmit",
                ),
            )

        self.assertTrue(process.terminated)
        self.assertFalse(process.killed)
        self.assertEqual(popen.call_args.kwargs["stderr"], subprocess.PIPE)


if __name__ == "__main__":
    unittest.main()
