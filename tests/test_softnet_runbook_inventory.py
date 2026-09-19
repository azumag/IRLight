from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "docs" / "operations" / "README.md"
RUNBOOK = ROOT / "docs" / "operations" / "softnet-pressure-monitoring.md"
HOST_AGGREGATE = ROOT / "scripts" / "check-host-pressure.sh"


class SoftnetRunbookInventoryTests(unittest.TestCase):
    def test_softnet_runbook_is_indexed_once(self) -> None:
        self.assertTrue(RUNBOOK.is_file())
        text = RUNBOOK.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# "))
        self.assertGreater(len(text.strip()), 200)

        index = INDEX.read_text(encoding="utf-8")
        self.assertEqual(index.count("](softnet-pressure-monitoring.md)"), 1)

    def test_softnet_runbook_keeps_read_only_baseline_contract(self) -> None:
        text = RUNBOOK.read_text(encoding="utf-8")
        self.assertIn("同じ host、network namespace、boot generation、CPU topology", text)
        self.assertIn("checker 単独では `CRITICAL` を返さない", text)
        self.assertIn("baseline は checker が作成・更新・削除しない", text)
        self.assertIn("cpu_topology_changed", (ROOT / "scripts" / "check-softnet-pressure-delta.sh").read_text(encoding="utf-8"))
        self.assertIn("counter_reset", text)
        self.assertIn("sysctl、qdisc、route、interface、socket、service、process、provider resource を変更しない", text)

    def test_softnet_host_aggregate_is_explicit_opt_in(self) -> None:
        runbook = RUNBOOK.read_text(encoding="utf-8")
        aggregate = HOST_AGGREGATE.read_text(encoding="utf-8")
        self.assertIn("IRLIGHT_HOST_SOFTNET_MODE=enabled", runbook)
        self.assertIn('softnet_mode="${IRLIGHT_HOST_SOFTNET_MODE:-disabled}"', aggregate)
        self.assertIn('add_component "softnet"', aggregate)
        self.assertIn("invalid_softnet_mode", aggregate)


if __name__ == "__main__":
    unittest.main()
