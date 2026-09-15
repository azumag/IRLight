from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "docs" / "operations" / "tcp-listen-overflow-monitoring.md"
INDEX = ROOT / "docs" / "operations" / "README.md"


class TcpListenOverflowRunbookContractTest(unittest.TestCase):
    def test_runbook_is_indexed_once(self) -> None:
        self.assertTrue(RUNBOOK.is_file())
        text = INDEX.read_text(encoding="utf-8")
        self.assertEqual(text.count("](tcp-listen-overflow-monitoring.md)"), 1)

    def test_runbook_keeps_read_only_baseline_contract(self) -> None:
        text = RUNBOOK.read_text(encoding="utf-8")
        self.assertIn("IRLIGHT_TCP_NETSTAT_BASELINE_PATH", text)
        self.assertIn("IRLIGHT_TCP_NETSTAT_PATH", text)
        self.assertIn("同じ host", text)
        self.assertIn("network namespace", text)
        self.assertIn("WARNING", text)
        self.assertIn("単独では `CRITICAL`", text)
        self.assertIn("baseline を作成・更新・削除", text)
        self.assertIn("ListenOverflows", text)
        self.assertIn("ListenDrops", text)


if __name__ == "__main__":
    unittest.main()
