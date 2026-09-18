from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "docs" / "operations" / "README.md"
RUNBOOK = ROOT / "docs" / "operations" / "oom-kill-monitoring.md"
SCRIPT = ROOT / "scripts" / "check-oom-kill-delta.sh"


class OomKillRunbookInventoryTests(unittest.TestCase):
    def test_oom_kill_runbook_is_indexed_once(self) -> None:
        self.assertTrue(RUNBOOK.is_file())
        text = RUNBOOK.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# "))
        self.assertGreater(len(text.strip()), 200)

        index = INDEX.read_text(encoding="utf-8")
        self.assertEqual(index.count("](oom-kill-monitoring.md)"), 1)

    def test_oom_kill_runbook_keeps_fail_closed_contract(self) -> None:
        text = RUNBOOK.read_text(encoding="utf-8")
        script = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("同じ host と boot generation", text)
        self.assertIn("checker 単独で `WARNING` には丸めず `CRITICAL`", text)
        self.assertIn("baseline は checker が作成・更新・削除しません", text)
        self.assertIn("counter_reset", script)
        self.assertIn("process、service、cgroup、sysctl、swap、filesystem、provider resource を変更しません", text)


if __name__ == "__main__":
    unittest.main()
