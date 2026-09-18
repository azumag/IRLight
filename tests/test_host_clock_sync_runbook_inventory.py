from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-host-clock-sync.py"
RUNBOOK = ROOT / "docs" / "operations" / "host-clock-sync-monitoring.md"
INDEX = ROOT / "docs" / "operations" / "README.md"
HOST_AGGREGATE = ROOT / "scripts" / "check-host-pressure.sh"


class HostClockSyncRunbookInventoryTests(unittest.TestCase):
    def test_runbook_is_indexed_and_documents_contract(self) -> None:
        self.assertTrue(SCRIPT.is_file())
        self.assertTrue(RUNBOOK.is_file())
        runbook = RUNBOOK.read_text(encoding="utf-8")
        index = INDEX.read_text(encoding="utf-8")

        self.assertIn("host-clock-sync-monitoring.md", index)
        self.assertIn("IRLIGHT_HOST_CLOCK_SYNC", runbook)
        self.assertIn("ntp_unsynchronized", runbook)
        self.assertIn("timedatectl_timeout", runbook)
        self.assertIn("read-only", runbook)
        self.assertIn("check-host-pressure.sh", runbook)

    def test_targeted_check_is_not_implicitly_added_to_host_aggregate(self) -> None:
        aggregate = HOST_AGGREGATE.read_text(encoding="utf-8")
        self.assertNotIn("check-host-clock-sync.py", aggregate)


if __name__ == "__main__":
    unittest.main()
