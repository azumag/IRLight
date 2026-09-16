from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import call, patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "network-tcp-reset-injector.py"
SPEC = importlib.util.spec_from_file_location("network_tcp_reset_interrupt", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class NetworkTcpResetInterruptTest(unittest.TestCase):
    def _plan(self):
        return MODULE.build_tcp_reset_plan(
            namespace="irlight-qa",
            destination="192.0.2.10",
            port=1935,
            rule_id="interrupt-case",
            duration_seconds=30,
        )

    def test_sleep_interrupt_still_cleans_up_and_returns_130(self) -> None:
        plan = self._plan()
        with (
            patch.object(MODULE, "_run") as run,
            patch.object(MODULE.time, "sleep", side_effect=KeyboardInterrupt),
        ):
            result = MODULE.apply_tcp_reset(
                namespace=plan.namespace,
                destination=plan.destination,
                port=plan.port,
                rule_id=plan.rule_id,
                duration_seconds=30,
                confirm_disposable_namespace=True,
            )

        self.assertEqual(result, 130)
        self.assertEqual(run.call_args_list, [call(plan.apply_argv), call(plan.cleanup_argv)])

    def test_apply_interrupt_still_attempts_cleanup_and_returns_130(self) -> None:
        plan = self._plan()
        with patch.object(
            MODULE,
            "_run",
            side_effect=(KeyboardInterrupt(), None),
        ) as run:
            result = MODULE.apply_tcp_reset(
                namespace=plan.namespace,
                destination=plan.destination,
                port=plan.port,
                rule_id=plan.rule_id,
                duration_seconds=30,
                confirm_disposable_namespace=True,
            )

        self.assertEqual(result, 130)
        self.assertEqual(run.call_args_list, [call(plan.apply_argv), call(plan.cleanup_argv)])


if __name__ == "__main__":
    unittest.main()
