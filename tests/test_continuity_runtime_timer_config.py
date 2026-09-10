from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
CONTINUITY_DIR = ROOT / "apps" / "continuity"
sys.path.insert(0, str(CONTINUITY_DIR))

from runtime_timer_config import RuntimeTimerConfigError, finite_env_float  # noqa: E402


class ContinuityRuntimeTimerConfigTest(unittest.TestCase):
    def test_missing_value_uses_finite_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(finite_env_float("SOURCE_RETRY_SECONDS", 3.0), 3.0)

    def test_existing_finite_value_semantics_are_preserved(self) -> None:
        cases = (("3", 3.0), ("0", 0.0), ("-1", -1.0), ("0.25", 0.25))
        for raw, expected in cases:
            with self.subTest(raw=raw):
                with patch.dict(
                    os.environ, {"SOURCE_RETRY_SECONDS": raw}, clear=True
                ):
                    self.assertEqual(
                        finite_env_float("SOURCE_RETRY_SECONDS", 3.0), expected
                    )

    def test_non_finite_values_are_rejected(self) -> None:
        values = (
            "NaN",
            "nan",
            "Infinity",
            "+Infinity",
            "-Infinity",
            "inf",
            "-inf",
        )
        for raw in values:
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
                cause = exc.__cause__
            else:
                self.fail("malformed timer unexpectedly accepted")

        self.assertEqual(message, "SOURCE_RETRY_SECONDS must be a finite number")
        self.assertNotIn(raw, message)
        self.assertIsNone(cause)

    def test_non_finite_default_is_also_rejected(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeTimerConfigError):
                finite_env_float("SOURCE_RETRY_SECONDS", float("inf"))

    def test_production_runner_validates_before_base_pipeline_initialization(self) -> None:
        source = (CONTINUITY_DIR / "runner.py").read_text(encoding="utf-8")
        validation = 'finite_env_float("SOURCE_RETRY_SECONDS", 3.0)'
        base_init = "super().__init__()"
        assignment = "self.source_retry_seconds = source_retry_seconds"

        self.assertIn(validation, source)
        self.assertIn(base_init, source)
        self.assertIn(assignment, source)
        self.assertLess(source.index(validation), source.index(base_init))
        self.assertGreater(source.index(assignment), source.index(base_init))

    def test_continuity_image_packages_timer_validator(self) -> None:
        dockerfile = (CONTINUITY_DIR / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("runtime_timer_config.py", dockerfile)
        self.assertIn('CMD ["python3", "-u", "/app/runner.py"]', dockerfile)


if __name__ == "__main__":
    unittest.main()
