from __future__ import annotations

import io
import socket
import subprocess
import sys
import threading
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


class DestinationProbeTest(unittest.TestCase):
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
        self.assertIn("conntimeo=2000", command[2])
        self.assertNotIn("-autoreconnect:no", command)
        self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.PIPE)
        self.assertEqual(result["protocol"], "srt")
        self.assertTrue(process.terminated)

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
