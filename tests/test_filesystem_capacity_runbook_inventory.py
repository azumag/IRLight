from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "docs" / "operations" / "README.md"
RUNBOOK = ROOT / "docs" / "operations" / "filesystem-capacity-monitoring.md"
SCRIPT = ROOT / "scripts" / "check-filesystem-capacity.py"


class FilesystemCapacityRunbookInventoryTests(unittest.TestCase):
    def test_filesystem_capacity_runbook_is_indexed_once(self) -> None:
        self.assertTrue(RUNBOOK.is_file())
        text = RUNBOOK.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# "))
        self.assertGreater(len(text.strip()), 400)

        index = INDEX.read_text(encoding="utf-8")
        self.assertEqual(index.count("](filesystem-capacity-monitoring.md)"), 1)

    def test_runbook_keeps_non_destructive_fail_closed_contract(self) -> None:
        text = RUNBOOK.read_text(encoding="utf-8")
        script = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("実際に 0", text)
        self.assertIn("warning threshold を決めません", text)
        self.assertIn("変更しません", text)
        self.assertIn("UNKNOWN", text)
        self.assertIn("blocks_exhausted", script)
        self.assertIn("inodes_exhausted", script)
        self.assertIn("path_unavailable", script)


if __name__ == "__main__":
    unittest.main()
