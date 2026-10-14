from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
EGRESS_DIR = ROOT / "apps" / "egress-gateway"

# apps/continuity ships its own runtime_timer_config module under the same
# top-level name, so a plain `from runtime_timer_config import ...` resolves
# to whichever one reached sys.modules first. Load this app's module by
# path, the same way test_egress_isolated_attempt.py loads isolated_attempt.py.
_spec = importlib.util.spec_from_file_location(
    "irlight_egress_runtime_timer_config", EGRESS_DIR / "runtime_timer_config.py"
)
if _spec is None or _spec.loader is None:
    raise RuntimeError("failed to load egress runtime timer config module")
RUNTIME_TIMER_CONFIG = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = RUNTIME_TIMER_CONFIG
_spec.loader.exec_module(RUNTIME_TIMER_CONFIG)

LIVENESS_TIMEOUT_NAME = RUNTIME_TIMER_CONFIG.LIVENESS_TIMEOUT_NAME
STATUS_HEARTBEAT_NAME = RUNTIME_TIMER_CONFIG.STATUS_HEARTBEAT_NAME
RuntimeTimerConfigError = RUNTIME_TIMER_CONFIG.RuntimeTimerConfigError
finite_env_float = RUNTIME_TIMER_CONFIG.finite_env_float
validate_liveness_window = RUNTIME_TIMER_CONFIG.validate_liveness_window


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

    def test_liveness_window_wider_than_status_heartbeat_is_accepted(self) -> None:
        # The default canary window must satisfy the relationship, otherwise
        # enabling the canary at all would be rejected at startup.
        for liveness, heartbeat in ((20.0, 5.0), (6.0, 5.0), (20.0, 0.5)):
            with self.subTest(liveness=liveness, heartbeat=heartbeat):
                validate_liveness_window(liveness, heartbeat)

    def test_liveness_window_not_wider_than_status_heartbeat_is_rejected(self) -> None:
        # A window no wider than the report cadence expires between two healthy
        # reports, fencing a healthy attempt into the RECONNECTING loop this
        # bound exists to shorten. Equal windows fail too: the window must be
        # strictly wider.
        for liveness, heartbeat in ((20.0, 20.0), (5.0, 20.0), (20.0, 30.0)):
            with self.subTest(liveness=liveness, heartbeat=heartbeat):
                with self.assertRaisesRegex(
                    RuntimeTimerConfigError,
                    rf"^{LIVENESS_TIMEOUT_NAME} must be greater than "
                    rf"{STATUS_HEARTBEAT_NAME}$",
                ):
                    validate_liveness_window(liveness, heartbeat)

    def test_disabled_status_heartbeat_rejects_an_armed_liveness_window(self) -> None:
        # With the heartbeat disabled the child has no post-connect channel to
        # emit liveness evidence, so any armed window would fence it blind.
        with self.assertRaises(RuntimeTimerConfigError):
            validate_liveness_window(20.0, 0.0)

    def test_disabled_liveness_window_is_not_constrained(self) -> None:
        # 0 keeps the historical unbounded post-connect behavior, so it is
        # accepted even alongside a disabled heartbeat.
        for liveness in (0.0, -1.0):
            with self.subTest(liveness=liveness):
                validate_liveness_window(liveness, 0.0)
                validate_liveness_window(liveness, 5.0)

    def test_liveness_window_rejection_echoes_no_configuration_value(self) -> None:
        try:
            validate_liveness_window(20.0, 30.0)
        except RuntimeTimerConfigError as exc:
            message = str(exc)
        else:
            self.fail("narrow liveness window unexpectedly accepted")
        # Only the variable names appear; neither raw value reaches the message.
        self.assertIn(LIVENESS_TIMEOUT_NAME, message)
        self.assertIn(STATUS_HEARTBEAT_NAME, message)
        self.assertNotIn("30", message)
        self.assertNotIn("20.0", message)

    def test_production_entrypoint_validates_liveness_window_relationship(self) -> None:
        source = (EGRESS_DIR / "egress_entrypoint.py").read_text(encoding="utf-8")
        self.assertIn("validate_liveness_window(", source)
        self.assertIn(STATUS_HEARTBEAT_NAME, source)
        # The canary hook must validate the relationship before it swaps in the
        # isolated attempt, and before any GStreamer pipeline is constructed.
        isolation = source.index("def _configure_attempt_isolation()")
        validation = source.index("validate_liveness_window(", isolation)
        wrapper = source.index("egress.EgressAttempt = IsolatedEgressAttempt")
        self.assertLess(validation, wrapper)

    def test_egress_image_packages_timer_validator(self) -> None:
        dockerfile = (EGRESS_DIR / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("runtime_timer_config.py", dockerfile)
        self.assertIn('CMD ["python3", "-u", "/app/egress_entrypoint.py"]', dockerfile)


if __name__ == "__main__":
    unittest.main()
