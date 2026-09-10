from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
EGRESS_DIR = ROOT / "apps" / "egress-gateway"
sys.path.insert(0, str(EGRESS_DIR))

from runtime_timer_config import RuntimeTimerConfigError, finite_env_float  # noqa: E402


RUNTIME_TIMERS = (
    ("EGRESS_CONNECT_TIMEOUT_SECONDS", 15.0),
    ("EGRESS_STATUS_HEARTBEAT_SECONDS", 5.0),
    ("EGRESS_CONNECT_STABILITY_SECONDS", 3.0),
    ("EGRESS_OUTPUT_STALL_TIMEOUT_SECONDS", 5.0),
)


class EgressRuntimeTimerConfigTest(unittest.TestCase):
    def test_missing_values_use_finite_defaults(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            for name, default in RUNTIME_TIMERS:
                with self.subTest(name=name):
                    self.assertEqual(finite_env_float(name, default), default)

    def test_existing_finite_value_semantics_are_preserved(self) -> None:
        cases = (("3", 3.0), ("0", 0.0), ("-1", -1.0), ("0.25", 0.25))
        for name, default in RUNTIME_TIMERS:
            for raw, expected in cases:
                with self.subTest(name=name, raw=raw):
                    with patch.dict(os.environ, {name: raw}, clear=True):
                        self.assertEqual(finite_env_float(name, default), expected)

    def test_non_finite_values_are_rejected_for_every_runtime_timer(self) -> None:
        values = (
            "NaN",
            "nan",
            "Infinity",
            "+Infinity",
            "-Infinity",
            "inf",
            "-inf",
        )
        for name, default in RUNTIME_TIMERS:
            for raw in values:
                with self.subTest(name=name, raw=raw):
                    with patch.dict(os.environ, {name: raw}, clear=True):
                        with self.assertRaisesRegex(
                            RuntimeTimerConfigError,
                            rf"^{name} must be a finite number$",
                        ):
                            finite_env_float(name, default)

    def test_malformed_value_is_rejected_without_echoing_raw_input(self) -> None:
        raw = "not-a-number-AUDIT_DUMMY_TOKEN"
        for name, default in RUNTIME_TIMERS:
            with self.subTest(name=name):
                with patch.dict(os.environ, {name: raw}, clear=True):
                    try:
                        finite_env_float(name, default)
                    except RuntimeTimerConfigError as exc:
                        message = str(exc)
                        cause = exc.__cause__
                    else:
                        self.fail("malformed timer unexpectedly accepted")

                self.assertEqual(message, f"{name} must be a finite number")
                self.assertNotIn(raw, message)
                self.assertIsNone(cause)

    def test_non_finite_default_is_also_rejected(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeTimerConfigError):
                finite_env_float("EGRESS_CONNECT_TIMEOUT_SECONDS", float("inf"))

    def test_production_entrypoint_validates_all_timers_before_egress_main(self) -> None:
        source = (EGRESS_DIR / "egress_entrypoint.py").read_text(encoding="utf-8")
        main_start = source.index("def main() -> int:")
        validation = source.index("        _validate_runtime_timers()", main_start)
        gateway_main = source.index("    return egress.main()", main_start)

        for name, _default in RUNTIME_TIMERS:
            self.assertIn(f'("{name}",', source)
        self.assertLess(validation, gateway_main)
        self.assertIn(
            'LOG.error("invalid finite egress runtime timer configuration")',
            source,
        )

    def test_egress_image_packages_timer_validator(self) -> None:
        dockerfile = (EGRESS_DIR / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("runtime_timer_config.py", dockerfile)
        self.assertIn('CMD ["python3", "-u", "/app/egress_entrypoint.py"]', dockerfile)


if __name__ == "__main__":
    unittest.main()
