from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "network-fault-injector.py"
SPEC = importlib.util.spec_from_file_location("network_fault_injector_type_guards", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
injector = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = injector
SPEC.loader.exec_module(injector)


class NetworkFaultLossTypeGuardTests(unittest.TestCase):
    def test_boolean_ordinary_loss_is_rejected(self) -> None:
        with self.assertRaisesRegex(injector.FaultPlanError, "loss_percent"):
            injector.build_fault_plan(
                interface="eth0",
                namespace="irlight-qa",
                loss_percent=True,
                latency_ms=None,
                jitter_ms=None,
                disconnect=False,
                duration_seconds=30,
            )

    def test_boolean_burst_loss_is_rejected(self) -> None:
        with self.assertRaisesRegex(injector.FaultPlanError, "burst_loss_percent"):
            injector.build_fault_plan(
                interface="eth0",
                namespace="irlight-qa",
                loss_percent=None,
                burst_loss_percent=True,
                burst_correlation_percent=50,
                latency_ms=None,
                jitter_ms=None,
                disconnect=False,
                duration_seconds=30,
            )


if __name__ == "__main__":
    unittest.main()
