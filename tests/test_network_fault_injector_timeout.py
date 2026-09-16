from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "network-fault-injector.py"
SPEC = importlib.util.spec_from_file_location("network_fault_injector_timeout", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
network_fault_injector = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = network_fault_injector
SPEC.loader.exec_module(network_fault_injector)


class NetworkFaultInjectorTimeoutTests(unittest.TestCase):
    def test_run_has_fixed_command_timeout(self) -> None:
        with mock.patch.object(
            network_fault_injector.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(["ip"], 10),
        ) as run:
            with self.assertRaisesRegex(RuntimeError, "could not complete safely"):
                network_fault_injector._run(("ip", "netns", "exec", "irlight-qa", "true"))

        self.assertEqual(run.call_args.kwargs["timeout"], network_fault_injector.COMMAND_TIMEOUT_SECONDS)
        self.assertFalse(run.call_args.kwargs["check"])

    def test_run_wraps_spawn_failure_without_command_output(self) -> None:
        with mock.patch.object(
            network_fault_injector.subprocess,
            "run",
            side_effect=OSError("synthetic spawn failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "could not complete safely"):
                network_fault_injector._run(("ip", "netns", "exec", "irlight-qa", "true"))


if __name__ == "__main__":
    unittest.main()
