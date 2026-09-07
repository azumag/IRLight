from __future__ import annotations

import io
import os
import socket
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from destination_probe import (  # noqa: E402
    DestinationProbeError,
    ProbeConfig,
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


class _UnstoppableSrtProcess:
    def __init__(self, clock: list[float]) -> None:
        self.clock = clock
        self.stderr = io.BytesIO(b"")
        self.stdin = io.BytesIO()
        self.terminated = False
        self.killed = False
        self.wait_timeouts: list[float] = []

    def poll(self) -> int | None:
        return None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        value = float(timeout or 0.0)
        self.wait_timeouts.append(value)
        self.clock[0] += value
        raise subprocess.TimeoutExpired("srt-live-transmit", timeout)

    def kill(self) -> None:
        self.killed = True


class _KillableSrtProcess(_UnstoppableSrtProcess):
    def __init__(self, clock: list[float]) -> None:
        super().__init__(clock)
        self._returncode: int | None = None

    def poll(self) -> int | None:
        return self._returncode

    def wait(self, timeout: float | None = None) -> int:
        value = float(timeout or 0.0)
        self.wait_timeouts.append(value)
        if not self.killed:
            self.clock[0] += value
            raise subprocess.TimeoutExpired("srt-live-transmit", timeout)
        self.clock[0] += min(value, 0.1)
        self._returncode = -9
        return self._returncode


class _HangingReader:
    def __init__(self) -> None:
        self.join_timeouts: list[float] = []

    def join(self, timeout: float | None = None) -> None:
        self.join_timeouts.append(float(timeout or 0.0))

    def is_alive(self) -> bool:
        return True


class _HangingResolverProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.killed = False
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO()
        self.communicate_timeouts: list[float] = []
        self.stdin_payloads: list[bytes | None] = []

    def communicate(
        self, input: bytes | None = None, timeout: float | None = None
    ) -> tuple[bytes, bytes]:
        self.stdin_payloads.append(input)
        self.communicate_timeouts.append(float(timeout or 0.0))
        if not self.killed:
            raise subprocess.TimeoutExpired("resolver", timeout)
        self.returncode = -9
        return b"", b""

    def kill(self) -> None:
        self.killed = True


class _ResolverResultProcess:
    def __init__(self, stdout: bytes) -> None:
        self._output = stdout
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO(stdout)
        self.returncode = 0

    def communicate(
        self, input: bytes | None = None, timeout: float | None = None
    ) -> tuple[bytes, bytes]:
        del input, timeout
        return self._output, b""


class _BudgetSocket:
    def __init__(
        self,
        clock: list[float],
        *,
        recv_step: float = 0.0,
        connect_step: float = 0.0,
        connect_error: Exception | None = None,
    ) -> None:
        self.clock = clock
        self.recv_step = recv_step
        self.connect_step = connect_step
        self.connect_error = connect_error
        self.timeouts: list[float] = []
        self.recv_calls = 0
        self.closed = False

    def settimeout(self, timeout: float) -> None:
        self.timeouts.append(timeout)

    def connect(self, sockaddr) -> None:
        del sockaddr
        self.clock[0] += self.connect_step
        if self.connect_error is not None:
            raise self.connect_error

    def sendall(self, data: bytes) -> None:
        del data

    def recv(self, size: int) -> bytes:
        del size
        self.clock[0] += self.recv_step
        self.recv_calls += 1
        return b"\x03" if self.recv_calls == 1 else b"S"

    def close(self) -> None:
        self.closed = True


class DestinationProbeTest(unittest.TestCase):
    def test_environment_timeout_nonfinite_values_use_safe_default(self) -> None:
        for raw in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(raw=raw), patch.dict(os.environ, {"IRLIGHT_VERIFY_TIMEOUT_SECONDS": raw}):
                self.assertEqual(ProbeConfig.from_env().timeout_seconds, 5.0)

    def test_direct_timeout_must_be_finite_positive_number(self) -> None:
        for value in (float("nan"), float("inf"), -float("inf"), 0, -1, True, None, "5"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ProbeConfig(timeout_seconds=value)

    @patch("destination_probe.ResolverPopen")
    def test_expired_dns_budget_does_not_spawn_child(self, resolver_popen) -> None:
        import destination_probe

        with patch("destination_probe.time.monotonic", return_value=1.0):
            with self.assertRaisesRegex(DestinationProbeError, "probe timed out"):
                destination_probe._resolve(
                    "probe.invalid", 1935, socktype=socket.SOCK_STREAM,
                    allow_private_targets=False, deadline=1.0,
                    timeout_message="RTMP destination probe timed out",
                )
        resolver_popen.assert_not_called()

    @patch("destination_probe.socket.socket")
    @patch("destination_probe.ResolverPopen")
    def test_dns_spawn_exhausting_budget_still_reaps_child(self, resolver_popen, socket_factory) -> None:
        clock = [0.0]
        process = _HangingResolverProcess()

        def spawn(*args, **kwargs):
            clock[0] = 1.0
            return process

        resolver_popen.side_effect = spawn
        with patch("destination_probe.time.monotonic", side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(DestinationProbeError, "probe timed out"):
                probe_destination("rtmp://probe.invalid/live", ProbeConfig(timeout_seconds=1.0))
        self.assertTrue(process.killed)
        self.assertEqual(process.returncode, -9)
        self.assertTrue(process.stdin.closed)
        self.assertTrue(process.stdout.closed)
        self.assertEqual(process.communicate_timeouts, [0.5])
        socket_factory.assert_not_called()

    @patch("destination_probe.socket.socket")
    @patch("destination_probe.ResolverPopen")
    def test_dns_communication_failure_still_reaps_child(self, resolver_popen, socket_factory) -> None:
        class BrokenPipeProcess(_HangingResolverProcess):
            def communicate(self, input=None, timeout=None):
                if not self.killed:
                    raise BrokenPipeError("test resolver pipe failure")
                return super().communicate(input=input, timeout=timeout)

        process = BrokenPipeProcess()
        resolver_popen.return_value = process
        with self.assertRaisesRegex(DestinationProbeError, "resolver is unavailable"):
            probe_destination("rtmp://probe.invalid/live", ProbeConfig(timeout_seconds=1.0))
        self.assertTrue(process.killed)
        self.assertEqual(process.returncode, -9)
        self.assertTrue(process.stdin.closed)
        self.assertTrue(process.stdout.closed)
        socket_factory.assert_not_called()

    @patch("destination_probe._resolve")
    @patch("destination_probe.socket.socket")
    def test_rtmp_final_send_must_finish_before_deadline(self, socket_factory, resolve) -> None:
        resolve.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("203.0.113.10", 1935))
        ]
        for final_send_seconds in (0.5, 1.0, 1.1):
            with self.subTest(final_send_seconds=final_send_seconds):
                clock = [0.0]

                class FinalSendSocket(_BudgetSocket):
                    sends = 0

                    def sendall(self, data):
                        self.sends += 1
                        if self.sends == 2:
                            self.clock[0] += final_send_seconds

                stream = FinalSendSocket(clock)
                socket_factory.return_value = stream
                with patch("destination_probe.time.monotonic", side_effect=lambda: clock[0]):
                    if final_send_seconds < 1.0:
                        result = probe_destination("rtmp://probe.invalid/live", ProbeConfig(timeout_seconds=1.0))
                        self.assertEqual(result["elapsed_ms"], 500.0)
                    else:
                        with self.assertRaisesRegex(DestinationProbeError, "probe timed out"):
                            probe_destination("rtmp://probe.invalid/live", ProbeConfig(timeout_seconds=1.0))
                self.assertTrue(stream.closed)

    def test_srt_cleanup_escalates_to_kill_within_shared_budget(self) -> None:
        import destination_probe

        clock = [0.0]
        process = _KillableSrtProcess(clock)

        with patch("destination_probe.time.monotonic", side_effect=lambda: clock[0]):
            destination_probe._cleanup_srt_process(process, None)

        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)
        self.assertEqual(process.poll(), -9)
        self.assertEqual(process.wait_timeouts, [0.25, 0.75])
        self.assertLess(clock[0], destination_probe.SRT_CLEANUP_SECONDS)
        self.assertTrue(process.stdin.closed)
        self.assertTrue(process.stderr.closed)

    def test_srt_cleanup_uses_one_shared_budget_without_blocking_on_reader(self) -> None:
        import destination_probe

        clock = [0.0]
        process = _UnstoppableSrtProcess(clock)
        reader = _HangingReader()

        with patch("destination_probe.time.monotonic", side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(DestinationProbeError, "cleanup timed out"):
                destination_probe._cleanup_srt_process(process, reader)

        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)
        self.assertTrue(process.stdin.closed)
        self.assertFalse(process.stderr.closed)
        self.assertEqual(process.wait_timeouts, [0.25, 0.75])
        self.assertEqual(reader.join_timeouts, [0.0])
        self.assertEqual(clock[0], destination_probe.SRT_CLEANUP_SECONDS)

    @patch("destination_probe._resolve")
    @patch("destination_probe.subprocess.Popen")
    @patch("destination_probe.queue.Queue")
    def test_srt_connected_at_exact_deadline_is_not_success(self, queue_factory, popen, resolve) -> None:
        import destination_probe

        clock = [0.0]
        process = _FakeProcess(b"SRT target connected")
        popen.return_value = process
        resolve.return_value = [
            (socket.AF_INET, socket.SOCK_DGRAM, 0, "", ("203.0.113.10", 8890))
        ]

        def connected_event(*args, **kwargs):
            clock[0] = 1.0
            return destination_probe.SRT_STDERR_EVENT_CONNECTED

        queue_factory.return_value.get.side_effect = connected_event
        with patch("destination_probe.time.monotonic", side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(DestinationProbeError, "handshake timed out"):
                probe_destination("srt://probe.invalid:8890", ProbeConfig(timeout_seconds=1.0))
        self.assertTrue(process.terminated)
        self.assertTrue(process.stdin.closed)
        self.assertTrue(process.stderr.closed)

    def test_rejects_private_target_by_default(self) -> None:
        with self.assertRaisesRegex(DestinationProbeError, "public address"):
            probe_destination(
                "rtmp://127.0.0.1:1935/live",
                ProbeConfig(timeout_seconds=1.0),
            )

    def test_rejects_embedded_credentials(self) -> None:
        with self.assertRaisesRegex(DestinationProbeError, "credentials"):
            probe_destination(
                "rtmp://user:password@127.0.0.1:1935/live",
                ProbeConfig(timeout_seconds=1.0, allow_private_targets=True),
            )

    def test_rtmp_performs_protocol_handshake(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        server_errors: list[Exception] = []

        def server() -> None:
            try:
                conn, _ = listener.accept()
                with conn:
                    client_hello = _recv_exact(conn, 1537)
                    self.assertEqual(client_hello[0], 3)
                    s1 = b"S" * 1536
                    s2 = client_hello[1:]
                    conn.sendall(b"\x03" + s1 + s2)
                    c2 = _recv_exact(conn, 1536)
                    self.assertEqual(c2, s1)
            except Exception as exc:
                server_errors.append(exc)
            finally:
                listener.close()

        thread = threading.Thread(target=server, daemon=True)
        thread.start()
        result = probe_destination(
            f"rtmp://127.0.0.1:{port}/live",
            ProbeConfig(timeout_seconds=2.0, allow_private_targets=True),
        )
        thread.join(timeout=2.0)
        self.assertEqual(server_errors, [])
        self.assertEqual(result["protocol"], "rtmp")
        self.assertEqual(result["peer_ip"], "127.0.0.1")
        self.assertEqual(result["peer_port"], port)

    @patch("destination_probe.socket.socket")
    @patch("destination_probe.ResolverPopen")
    def test_rtmp_dns_lookup_is_killed_at_shared_deadline(
        self, resolver_popen, socket_factory
    ) -> None:
        process = _HangingResolverProcess()
        resolver_popen.return_value = process

        with self.assertRaisesRegex(DestinationProbeError, "probe timed out"):
            probe_destination(
                "rtmp://probe.invalid:1935/live",
                ProbeConfig(timeout_seconds=1.0),
            )

        self.assertTrue(process.killed)
        self.assertGreater(process.communicate_timeouts[0], 0.0)
        self.assertLessEqual(process.communicate_timeouts[0], 1.0)
        self.assertIn(b'"host":"probe.invalid"', process.stdin_payloads[0] or b"")
        command = resolver_popen.call_args.args[0]
        self.assertNotIn("probe.invalid", command)
        socket_factory.assert_not_called()

    @patch("destination_probe.socket.socket")
    def test_hung_dns_child_is_hard_bounded(self, socket_factory) -> None:
        import destination_probe

        started = time.monotonic()
        with patch(
            "destination_probe._DNS_RESOLVER_PROGRAM",
            "import time; time.sleep(60)",
        ):
            with self.assertRaisesRegex(DestinationProbeError, "probe timed out"):
                probe_destination(
                    "rtmp://probe.invalid:1935/live",
                    ProbeConfig(timeout_seconds=0.15),
                )
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1.0)
        socket_factory.assert_not_called()
        self.assertEqual(destination_probe.DNS_RESOLVER_CLEANUP_SECONDS, 0.5)

    @patch("destination_probe.ResolverPopen")
    def test_dns_answer_overflow_fails_closed(self, resolver_popen) -> None:
        resolver_popen.return_value = _ResolverResultProcess(
            b'{"status":"overflow"}'
        )

        with self.assertRaisesRegex(DestinationProbeError, "safety limit"):
            probe_destination(
                "rtmp://probe.invalid:1935/live",
                ProbeConfig(timeout_seconds=1.0),
            )

    @patch("destination_probe._resolve")
    @patch("destination_probe.socket.socket")
    def test_rtmp_slow_drip_cannot_reset_total_deadline(self, socket_factory, resolve) -> None:
        clock = [0.0]
        fake_socket = _BudgetSocket(clock, recv_step=0.4)
        socket_factory.return_value = fake_socket
        resolve.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("203.0.113.10", 1935))
        ]

        with patch("destination_probe.time.monotonic", side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(DestinationProbeError, "probe timed out"):
                probe_destination(
                    "rtmp://probe.invalid:1935/live",
                    ProbeConfig(timeout_seconds=1.0),
                )

        self.assertLess(fake_socket.recv_calls, 10)
        self.assertTrue(fake_socket.closed)

    @patch("destination_probe._resolve")
    @patch("destination_probe.socket.socket")
    def test_rtmp_address_attempts_share_one_deadline(self, socket_factory, resolve) -> None:
        clock = [0.0]
        sockets: list[_BudgetSocket] = []

        def make_socket(*args):
            del args
            item = _BudgetSocket(
                clock,
                connect_step=0.6,
                connect_error=socket.timeout("simulated timeout"),
            )
            sockets.append(item)
            return item

        socket_factory.side_effect = make_socket
        resolve.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("203.0.113.10", 1935)),
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("203.0.113.11", 1935)),
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("203.0.113.12", 1935)),
        ]

        with patch("destination_probe.time.monotonic", side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(DestinationProbeError, "probe timed out"):
                probe_destination(
                    "rtmp://probe.invalid:1935/live",
                    ProbeConfig(timeout_seconds=1.0),
                )

        self.assertEqual(len(sockets), 2)
        self.assertAlmostEqual(sockets[0].timeouts[0], 1.0)
        self.assertAlmostEqual(sockets[1].timeouts[0], 0.4)

    @patch("destination_probe._resolve")
    @patch("destination_probe.socket.socket")
    def test_rtmp_resolution_time_consumes_shared_budget(self, socket_factory, resolve) -> None:
        clock = [0.0]

        def delayed_resolve(*args, **kwargs):
            del args, kwargs
            clock[0] = 1.1
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("203.0.113.10", 1935))
            ]

        resolve.side_effect = delayed_resolve
        with patch("destination_probe.time.monotonic", side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(DestinationProbeError, "probe timed out"):
                probe_destination(
                    "rtmp://probe.invalid:1935/live",
                    ProbeConfig(timeout_seconds=1.0),
                )

        socket_factory.assert_not_called()

    @patch("destination_probe._probe_rtmp")
    def test_rtmps_uses_tls_probe(self, probe_rtmp) -> None:
        probe_rtmp.return_value = {
            "protocol": "rtmps",
            "peer_ip": "203.0.113.10",
            "peer_port": 443,
            "elapsed_ms": 1.0,
        }
        result = probe_destination(
            "rtmps://stream.example.com/app",
            ProbeConfig(timeout_seconds=2.0),
        )
        self.assertEqual(result["protocol"], "rtmps")
        self.assertTrue(probe_rtmp.call_args.kwargs["use_tls"])
        self.assertEqual(probe_rtmp.call_args.kwargs["port"], 443)

    @patch("destination_probe.subprocess.Popen")
    def test_srt_waits_for_real_connected_event_and_uses_literal_ip(self, popen) -> None:
        process = _FakeProcess(
            b"Media path: 'file://con' --> 'srt://127.0.0.1:8890'\n"
            b"Target connected (caller)\n"
        )
        popen.return_value = process

        result = probe_destination(
            "srt://127.0.0.1:8890?streamid=publish:probe&latency=120",
            ProbeConfig(
                timeout_seconds=2.0,
                allow_private_targets=True,
                srt_binary="srt-live-transmit",
            ),
        )

        command = popen.call_args.args[0]
        self.assertEqual(command[0], "srt-live-transmit")
        self.assertEqual(command[1], "file://con")
        self.assertIn("srt://127.0.0.1:8890?", command[2])
        self.assertIn("streamid=publish:probe", command[2])
        self.assertIn("mode=caller", command[2])
        conntimeo = next(
            token for token in command[2].split("&") if token.startswith("conntimeo=")
        )
        self.assertGreaterEqual(int(conntimeo.split("=", 1)[1]), 1)
        self.assertLessEqual(int(conntimeo.split("=", 1)[1]), 2000)
        self.assertNotIn("-autoreconnect:no", command)
        self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.PIPE)
        self.assertEqual(result["protocol"], "srt")
        self.assertTrue(process.terminated)

    @patch("destination_probe._resolve")
    @patch("destination_probe.subprocess.Popen")
    def test_srt_resolution_time_consumes_budget_before_process_spawn(self, popen, resolve) -> None:
        clock = [0.0]

        def delayed_resolve(*args, **kwargs):
            del args, kwargs
            clock[0] = 1.1
            return [
                (socket.AF_INET, socket.SOCK_DGRAM, 0, "", ("203.0.113.10", 8890))
            ]

        resolve.side_effect = delayed_resolve
        with patch("destination_probe.time.monotonic", side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(DestinationProbeError, "SRT destination handshake timed out"):
                probe_destination(
                    "srt://probe.invalid:8890?streamid=publish:probe",
                    ProbeConfig(timeout_seconds=1.0),
                )

        popen.assert_not_called()

    @patch("destination_probe.subprocess.Popen")
    def test_srt_fails_when_process_ends_before_connected_event(self, popen) -> None:
        popen.return_value = _FakeProcess(b"ERROR: Connection setup failure\n")

        with self.assertRaisesRegex(DestinationProbeError, "SRT handshake"):
            probe_destination(
                "srt://127.0.0.1:8890?streamid=publish:probe",
                ProbeConfig(
                    timeout_seconds=1.0,
                    allow_private_targets=True,
                    srt_binary="srt-live-transmit",
                ),
            )

    def test_srt_rejects_passphrase_in_url(self) -> None:
        with self.assertRaisesRegex(DestinationProbeError, "secrets"):
            probe_destination(
                "srt://127.0.0.1:8890?passphrase=topsecret123",
                ProbeConfig(timeout_seconds=1.0, allow_private_targets=True),
            )

    @patch("destination_probe.subprocess.Popen")
    def test_srt_rejects_authenticated_streamid_before_process_spawn(self, popen) -> None:
        with self.assertRaisesRegex(DestinationProbeError, "authenticated SRT streamid"):
            probe_destination(
                "srt://127.0.0.1:8890?streamid="
                "publish:live/input:dummy-user:AUDIT_DUMMY_SECRET",
                ProbeConfig(timeout_seconds=1.0, allow_private_targets=True),
            )
        popen.assert_not_called()

    @patch("destination_probe.subprocess.Popen")
    def test_srt_rejects_double_encoded_authenticated_streamid(self, popen) -> None:
        with self.assertRaisesRegex(DestinationProbeError, "authenticated SRT streamid"):
            probe_destination(
                "srt://127.0.0.1:8890?streamid="
                "publish%253Alive%252Finput%253Adummy-user%253AAUDIT_DUMMY_SECRET",
                ProbeConfig(timeout_seconds=1.0, allow_private_targets=True),
            )
        popen.assert_not_called()

    @patch("destination_probe.subprocess.Popen")
    def test_srt_rejects_structured_streamid_credentials(self, popen) -> None:
        with self.assertRaisesRegex(DestinationProbeError, "authenticated SRT streamid"):
            probe_destination(
                "srt://127.0.0.1:8890?streamid="
                "%23!%3A%3Ar=live%2Finput%2Cm=publish%2Cu=dummy-user%2Cpassword=AUDIT_DUMMY_SECRET",
                ProbeConfig(timeout_seconds=1.0, allow_private_targets=True),
            )
        popen.assert_not_called()

    @patch("destination_probe.subprocess.Popen")
    def test_srt_rejects_duplicate_query_parameters(self, popen) -> None:
        with self.assertRaisesRegex(DestinationProbeError, "duplicate query parameters"):
            probe_destination(
                "srt://127.0.0.1:8890?streamid=publish:probe&STREAMID=publish:other",
                ProbeConfig(timeout_seconds=1.0, allow_private_targets=True),
            )
        popen.assert_not_called()

    @patch("destination_probe.subprocess.Popen")
    def test_srt_rejects_unknown_query_parameter(self, popen) -> None:
        with self.assertRaisesRegex(DestinationProbeError, "unsupported query parameter"):
            probe_destination(
                "srt://127.0.0.1:8890?streamid=publish:probe&futuresecret=AUDIT_DUMMY_SECRET",
                ProbeConfig(timeout_seconds=1.0, allow_private_targets=True),
            )
        popen.assert_not_called()

    def test_srt_requires_caller_mode(self) -> None:
        with self.assertRaisesRegex(DestinationProbeError, "caller mode"):
            probe_destination(
                "srt://127.0.0.1:8890?mode=listener",
                ProbeConfig(timeout_seconds=1.0, allow_private_targets=True),
            )


def _recv_exact(stream: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.recv(remaining)
        if not chunk:
            raise RuntimeError("unexpected EOF")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


if __name__ == "__main__":
    unittest.main()
