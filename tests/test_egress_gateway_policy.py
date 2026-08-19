from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "egress-gateway"))

from egress_policy import ReconnectPolicy, classify_error, safe_destination  # noqa: E402


class ReconnectPolicyTest(unittest.TestCase):
    def test_exponential_backoff_is_capped(self) -> None:
        policy = ReconnectPolicy(
            initial_seconds=1,
            max_seconds=5,
            multiplier=2,
            jitter_ratio=0,
        )
        self.assertEqual([policy.delay_for(i) for i in range(1, 6)], [1, 2, 4, 5, 5])

    def test_jitter_stays_inside_configured_range(self) -> None:
        policy = ReconnectPolicy(
            initial_seconds=10,
            max_seconds=30,
            multiplier=2,
            jitter_ratio=0.2,
        )
        self.assertEqual(policy.delay_for(1, 0.0), 8.0)
        self.assertEqual(policy.delay_for(1, 1.0), 12.0)

    def test_attempt_and_elapsed_limits(self) -> None:
        policy = ReconnectPolicy(max_attempts=3, max_elapsed_seconds=20)
        self.assertFalse(policy.exhausted(2, 19.9))
        self.assertTrue(policy.exhausted(3, 1))
        self.assertTrue(policy.exhausted(1, 20))

    def test_invalid_policy_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ReconnectPolicy(initial_seconds=5, max_seconds=1)
        with self.assertRaises(ValueError):
            ReconnectPolicy(jitter_ratio=1.1)


class ErrorClassificationTest(unittest.TestCase):
    def test_upstream_rtspsrc_error_is_not_misclassified_as_destination(self) -> None:
        self.assertEqual(
            classify_error(source_name="src", message="connection refused"),
            "UPSTREAM_UNAVAILABLE",
        )

    def test_terminal_destination_failures(self) -> None:
        self.assertEqual(
            classify_error(source_name="egress_sink", message="Server returned 401 Unauthorized"),
            "AUTH_FAILED",
        )
        self.assertEqual(
            classify_error(source_name="egress_sink", message="NetStream.Publish.BadName"),
            "PUBLISH_CONFLICT",
        )

    def test_transient_destination_failures(self) -> None:
        cases = [
            ("certificate verify failed", "TLS_FAILED"),
            ("could not resolve host", "DNS_FAILED"),
            ("operation timed out", "TIMEOUT"),
            ("connection refused", "UNREACHABLE"),
            ("unexpected transport failure", "EGRESS_PIPELINE_FAILED"),
        ]
        for message, expected in cases:
            with self.subTest(message=message):
                self.assertEqual(
                    classify_error(source_name="egress_sink", message=message), expected
                )

    def test_safe_destination_exposes_only_scheme_and_host(self) -> None:
        scheme, host = safe_destination("rtmps://live.example/app/secret-key?token=hidden")
        self.assertEqual((scheme, host), ("rtmps", "live.example"))


if __name__ == "__main__":
    unittest.main()
