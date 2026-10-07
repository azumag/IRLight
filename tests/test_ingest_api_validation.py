from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from ingest_api import (  # noqa: E402
    ACCEPTING_INGEST_STATES,
    INGEST_CLOCK_UNAVAILABLE,
    INGEST_DEADLINE_UNAVAILABLE,
    IssueIngestCredentialRequest,
    _cache_valid_until,
    _validated_ingest_clock,
    _validated_ingest_deadline,
)


class IngestApiValidationTest(unittest.TestCase):
    def test_protocol_list_must_not_be_empty(self) -> None:
        with self.assertRaises(ValidationError):
            IssueIngestCredentialRequest(protocols=[])

    def test_unknown_protocol_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            IssueIngestCredentialRequest(protocols=["rtsp"])

    def test_degraded_session_remains_eligible_for_ingest_auth(self) -> None:
        self.assertIn("DEGRADED", ACCEPTING_INGEST_STATES)

    def test_cache_deadline_uses_shorter_of_credential_expiry_and_max_age(self) -> None:
        with patch.dict(
            os.environ,
            {"IRLIGHT_INGEST_AUTH_CACHE_MAX_AGE_SECONDS": "300"},
        ):
            self.assertEqual(
                _cache_valid_until({"expires_at": 1000.0}, now=100.0),
                400.0,
            )
            self.assertEqual(
                _cache_valid_until({"expires_at": 250.0}, now=100.0),
                250.0,
            )

    def test_cache_deadline_rejects_non_numeric_or_nonfinite_expiry(self) -> None:
        invalid_values = (
            None,
            True,
            "250",
            float("nan"),
            float("inf"),
            -float("inf"),
            -1,
            10**10000,
        )
        for index, value in enumerate(invalid_values):
            with self.subTest(case=index, value_type=type(value).__name__):
                self.assertIsNone(
                    _cache_valid_until({"expires_at": value}, now=100.0)
                )

    def test_cache_deadline_rejects_invalid_control_plane_clock(self) -> None:
        invalid_values = (
            True,
            "100",
            float("nan"),
            float("inf"),
            -float("inf"),
            -1,
            10**10000,
        )
        for index, value in enumerate(invalid_values):
            with self.subTest(case=index, value_type=type(value).__name__):
                self.assertIsNone(
                    _cache_valid_until({"expires_at": 1000.0}, now=value)
                )

    def test_cache_deadline_does_not_prime_expired_credential(self) -> None:
        self.assertIsNone(_cache_valid_until({"expires_at": 100.0}, now=100.0))
        self.assertIsNone(_cache_valid_until({"expires_at": 99.0}, now=100.0))

    def test_ingest_clock_rejects_invalid_values_before_deadline_math(self) -> None:
        invalid_values = (
            True,
            "100",
            float("nan"),
            float("inf"),
            -float("inf"),
            -1,
            10**10000,
        )
        for index, value in enumerate(invalid_values):
            with self.subTest(case=index, value_type=type(value).__name__):
                with self.assertRaises(HTTPException) as failure:
                    _validated_ingest_clock(value)
                self.assertEqual(failure.exception.status_code, 503)
                self.assertEqual(
                    failure.exception.detail, INGEST_CLOCK_UNAVAILABLE
                )

    def test_ingest_clock_accepts_epoch_zero_and_positive_values(self) -> None:
        self.assertEqual(_validated_ingest_clock(0.0), 0.0)
        self.assertEqual(_validated_ingest_clock(0), 0.0)
        self.assertEqual(_validated_ingest_clock(1.5), 1.5)

    def test_ingest_deadline_rejects_values_that_cannot_cap_a_credential(self) -> None:
        invalid_values = (
            None,
            True,
            "100",
            float("nan"),
            float("inf"),
            -float("inf"),
            -1,
            10**10000,
        )
        for index, value in enumerate(invalid_values):
            with self.subTest(case=index, value_type=type(value).__name__):
                with self.assertRaises(HTTPException) as failure:
                    _validated_ingest_deadline(value)
                self.assertEqual(failure.exception.status_code, 503)
                self.assertEqual(
                    failure.exception.detail, INGEST_DEADLINE_UNAVAILABLE
                )

    def test_ingest_deadline_accepts_epoch_zero_and_positive_values(self) -> None:
        self.assertEqual(_validated_ingest_deadline(0.0), 0.0)
        self.assertEqual(_validated_ingest_deadline(0), 0.0)
        self.assertEqual(_validated_ingest_deadline(10.5), 10.5)


if __name__ == "__main__":
    unittest.main()
