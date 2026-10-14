from __future__ import annotations

import ast
import importlib.util
import math
import sys
import unittest
from pathlib import Path


EGRESS_DIR = Path(__file__).resolve().parents[1] / "apps" / "egress-gateway"
_spec = importlib.util.spec_from_file_location(
    "irlight_egress_report_cadence_target", EGRESS_DIR / "egress_report_cadence.py"
)
if _spec is None or _spec.loader is None:
    raise RuntimeError("failed to load egress report cadence module")
CADENCE = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(CADENCE)


class EgressReportCadenceTest(unittest.TestCase):
    def test_heartbeat_rounds_up_to_the_existing_quarter_second_poll(self) -> None:
        cases = (
            (math.nextafter(0.0, math.inf), 0.25),
            (0.1, 0.25),
            (math.nextafter(0.25, 0.0), 0.25),
            (0.25, 0.25),
            (math.nextafter(0.25, math.inf), 0.5),
            (0.3, 0.5),
            (0.5, 0.5),
            (math.nextafter(0.5, math.inf), 0.75),
            (5.0, 5.0),
            (math.nextafter(2.0**50, 0.0), 2.0**50),
            (2.0**50, 2.0**50),
            (math.nextafter(2.0**50, math.inf), math.nextafter(2.0**50, math.inf)),
            (sys.float_info.max, sys.float_info.max),
        )
        for heartbeat, expected in cases:
            with self.subTest(heartbeat=heartbeat):
                actual = CADENCE.heartbeat_report_interval_seconds(heartbeat)
                self.assertEqual(actual, expected)
                self.assertTrue(math.isfinite(actual))

    def test_liveness_window_must_exceed_the_poll_rounded_interval(self) -> None:
        for heartbeat, interval in ((0.1, 0.25), (0.3, 0.5), (5.0, 5.0)):
            for timeout, expected in (
                (math.nextafter(interval, 0.0), False),
                (interval, False),
                (math.nextafter(interval, math.inf), True),
            ):
                with self.subTest(heartbeat=heartbeat, timeout=timeout):
                    self.assertEqual(
                        CADENCE.liveness_window_can_be_armed(timeout, heartbeat),
                        expected,
                    )

    def test_disabled_timers_and_defaults_keep_their_existing_meaning(self) -> None:
        self.assertEqual(CADENCE.SINK_POLL_MILLISECONDS, 250)
        self.assertEqual(CADENCE.heartbeat_report_interval_seconds(0.0), 0.0)
        self.assertFalse(CADENCE.liveness_window_can_be_armed(20.0, 0.0))
        self.assertFalse(CADENCE.liveness_window_can_be_armed(0.0, 5.0))
        self.assertTrue(CADENCE.liveness_window_can_be_armed(20.0, 5.0))
        self.assertFalse(
            CADENCE.liveness_window_can_be_armed(sys.float_info.max, sys.float_info.max)
        )

    def test_real_sink_poll_uses_the_shared_existing_interval(self) -> None:
        tree = ast.parse((EGRESS_DIR / "egress.py").read_text(encoding="utf-8"))
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "GLib"
            and node.func.attr == "timeout_add"
            and len(node.args) == 2
            and isinstance(node.args[1], ast.Attribute)
            and node.args[1].attr == "_poll_sink"
        ]
        self.assertEqual(len(calls), 1)
        self.assertEqual(ast.dump(calls[0].args[0]), "Name(id='SINK_POLL_MILLISECONDS', ctx=Load())")


if __name__ == "__main__":
    unittest.main()
