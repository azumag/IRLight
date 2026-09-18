from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "docs" / "operations" / "README.md"
RUNBOOK = ROOT / "docs" / "operations" / "softnet-pressure-monitoring.md"


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


if __name__ == "__main__":
    unittest.main()
