from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "docs" / "operations" / "README.md"
RUNBOOK = ROOT / "docs" / "operations" / "host-swap-pressure-monitoring.md"
SCRIPT = ROOT / "scripts" / "check-host-swap-pressure.sh"
AGGREGATE = ROOT / "scripts" / "check-host-pressure.sh"


class HostSwapPressureRunbookInventoryTest(unittest.TestCase):
    def test_operations_index_links_runbook_once(self) -> None:
        index = INDEX.read_text(encoding="utf-8")
        self.assertEqual(index.count("](host-swap-pressure-monitoring.md)"), 1)

    def test_runbook_captures_command_policy_and_safety_boundary(self) -> None:
        text = RUNBOOK.read_text(encoding="utf-8")
        self.assertIn("scripts/check-host-swap-pressure.sh", text)
        self.assertIn("IRLIGHT_SWAP_WARNING_PERCENT", text)
        self.assertIn("IRLIGHT_SWAP_CRITICAL_PERCENT", text)
        self.assertIn("status=OK usage_percent=NA", text)
        self.assertIn("IRLIGHT_HOST_SWAP_PRESSURE_MODE", text)
        self.assertIn("swap_pressure_status=OK|WARNING|CRITICAL|UNKNOWN", text)
        self.assertIn("invalid_swap_pressure_mode", text)
        self.assertIn("swapon", text)
        self.assertIn("swapoff", text)
        self.assertIn("自動変更しない", text)
        self.assertTrue(SCRIPT.is_file())
        self.assertTrue(AGGREGATE.is_file())

    def test_host_aggregate_keeps_swap_pressure_opt_in(self) -> None:
        text = AGGREGATE.read_text(encoding="utf-8")
        self.assertIn('IRLIGHT_HOST_SWAP_PRESSURE_MODE:-disabled', text)
        self.assertIn('add_component "swap_pressure"', text)
        self.assertIn('reason=invalid_swap_pressure_mode', text)


if __name__ == "__main__":
    unittest.main()
