from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "continuity"))

from runtime_timer_config import RuntimeTimerConfigError, finite_env_float  # noqa: E402


class ContinuityRuntimeTimerConfigTest(unittest.TestCase):
    def test_missing_value_uses_finite_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(finite_env_float("SOURCE_RETRY_SECONDS", 3.0), 3.0)

    def test_existing_finite_value_semantics_are_preserved(self) -> None:
        for raw, expected in (("3", 3.0), ("0", 0.0), ("-1", -1.0), ("0.25", 0.25)):
            with self.subTest(raw=raw):
                with patch.dict(
                    os.environ, {"SOURCE_RETRY_SECONDS": raw}, clear=True
                ):
                    self.assertEqual(
                        finite_env_float("SOURCE_RETRY_SECONDS", 3.0), expected
                    )

    def test_non_finite_values_are_rejected(self) -> None:
        for raw in ("NaN", "nan", "Infinity", "+Infinity", "-Infinity", "inf", "-inf"):
            with self.subTest(raw=raw):
                with patch.dict(
                    os.environ, {"SOURCE_RETRY_SECONDS": raw}, clear=True
                ):
                    with self.assertRaisesRegex(
                        RuntimeTimerConfigError,
                        r"^SOURCE_RETRY_SECONDS must be a finite number$",
                    ):
                        finite_env_float("SOURCE_RETRY_SECONDS", 3.0)

    def test_malformed_value_is_rejected_without_echoing_raw_input(self) -> None:
        raw = "not-a-number-AUDIT_DUMMY_TOKEN"
        with patch.dict(os.environ, {"SOURCE_RETRY_SECONDS": raw}, clear=True):
            try:
                finite_env_float("SOURCE_RETRY_SECONDS", 3.0)
            except RuntimeTimerConfigError as exc:
                message = str(exc)
            else:
                self.fail("malformed timer unexpectedly accepted")

        self.assertEqual(message, "SOURCE_RETRY_SECONDS must be a finite number")
        self.assertNotIn(raw, message)

    def test_non_finite_default_is_also_rejected(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeTimerConfigError):
                finite_env_float("SOURCE_RETRY_SECONDS", float("inf"))


if __name__ == "__main__":
    unittest.main()
