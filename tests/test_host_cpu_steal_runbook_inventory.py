from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "docs" / "operations" / "README.md"
RUNBOOK = ROOT / "docs" / "operations" / "host-cpu-steal-monitoring.md"
SCRIPT = ROOT / "scripts" / "check-cpu-steal-delta.sh"
HOST_AGGREGATE = ROOT / "scripts" / "check-host-pressure.sh"


class HostCpuStealRunbookInventoryTests(unittest.TestCase):
    def test_runbook_is_indexed_once(self) -> None:
        self.assertTrue(RUNBOOK.is_file())
        text = RUNBOOK.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# "))
        self.assertGreater(len(text.strip()), 500)

        index = INDEX.read_text(encoding="utf-8")
        self.assertEqual(index.count("](host-cpu-steal-monitoring.md)"), 1)

    def test_runbook_keeps_targeted_read_only_contract(self) -> None:
        text = RUNBOOK.read_text(encoding="utf-8")
        script = SCRIPT.read_text(encoding="utf-8")
        host_aggregate = HOST_AGGREGATE.read_text(encoding="utf-8")

        self.assertIn("cpu_steal_activity", text)
        self.assertIn("baseline は checker が作成・更新しない", text)
        self.assertIn("既定の `check-host-pressure.sh` aggregate へ自動追加しない", text)
        self.assertIn("check-host-boot-generation.sh", text)
        self.assertIn("status=WARNING reason=cpu_steal_activity", script)
        self.assertNotIn("check-cpu-steal-delta.sh", host_aggregate)


if __name__ == "__main__":
    unittest.main()
