from __future__ import annotations

import socket
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "egress-gateway"))

from destination_guard import (  # noqa: E402
    DestinationGuardError,
    read_verified_peer_ip,
    validate_destination_runtime,
)


def _answers(*addresses: str):
    return [
        (socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 1935))
        for address in addresses
    ]


class DestinationGuardTest(unittest.TestCase):
    def test_allows_public_answer_matching_verified_peer(self) -> None:
        result = validate_destination_runtime(
            "rtmp://stream.example/live/key",
            expected_peer_ip="8.8.8.8",
            resolver=lambda *_args, **_kwargs: _answers("8.8.8.8", "1.1.1.1"),
        )
        self.assertEqual(result.host, "stream.example")
        self.assertEqual(result.port, 1935)
        self.assertEqual(result.addresses, ("8.8.8.8", "1.1.1.1"))

    def test_rejects_cloud_metadata_link_local_address(self) -> None:
        with self.assertRaises(DestinationGuardError) as failure:
            validate_destination_runtime(
                "rtmp://metadata.example/live/key",
                resolver=lambda *_args, **_kwargs: _answers("169.254.169.254"),
            )
        self.assertEqual(failure.exception.reason_code, "DESTINATION_UNSAFE")
        self.assertTrue(failure.exception.terminal)

    def test_rejects_mixed_public_and_private_answers(self) -> None:
        with self.assertRaises(DestinationGuardError) as failure:
            validate_destination_runtime(
                "rtmp://mixed.example/live/key",
                resolver=lambda *_args, **_kwargs: _answers("8.8.8.8", "10.0.0.7"),
            )
        self.assertEqual(failure.exception.reason_code, "DESTINATION_UNSAFE")

    def test_private_targets_require_explicit_override(self) -> None:
        result = validate_destination_runtime(
            "rtmp://mediamtx:1935/live/key",
            allow_private_targets=True,
            resolver=lambda *_args, **_kwargs: _answers("172.18.0.5"),
        )
        self.assertEqual(result.addresses, ("172.18.0.5",))

    def test_rejects_dns_drift_after_verification(self) -> None:
        with self.assertRaises(DestinationGuardError) as failure:
            validate_destination_runtime(
                "rtmps://stream.example/app/key",
                expected_peer_ip="8.8.8.8",
                resolver=lambda *_args, **_kwargs: _answers("1.1.1.1"),
            )
        self.assertEqual(failure.exception.reason_code, "DESTINATION_DNS_CHANGED")
        self.assertTrue(failure.exception.terminal)

    def test_dns_resolution_failure_is_retryable(self) -> None:
        def fail(*_args, **_kwargs):
            raise socket.gaierror(socket.EAI_AGAIN, "temporary failure")

        with self.assertRaises(DestinationGuardError) as failure:
            validate_destination_runtime("rtmp://stream.example/live/key", resolver=fail)
        self.assertEqual(failure.exception.reason_code, "DNS_FAILED")
        self.assertFalse(failure.exception.terminal)

    def test_verified_peer_file_is_optional_and_validated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "peer"
            self.assertIsNone(read_verified_peer_ip(path))
            path.write_text("8.8.8.8\n", encoding="utf-8")
            self.assertEqual(read_verified_peer_ip(path), "8.8.8.8")
            path.write_text("not-an-ip\n", encoding="utf-8")
            with self.assertRaises(DestinationGuardError) as failure:
                read_verified_peer_ip(path)
            self.assertEqual(failure.exception.reason_code, "DESTINATION_GUARD_INVALID")


if __name__ == "__main__":
    unittest.main()
