from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "docs" / "operations" / "README.md"
RUNBOOK = ROOT / "docs" / "operations" / "host-boot-generation-monitoring.md"
SCRIPT = ROOT / "scripts" / "check-host-boot-generation.sh"
HOST_AGGREGATE = ROOT / "scripts" / "check-host-pressure.sh"


class HostBootGenerationRunbookInventoryTests(unittest.TestCase):
    def test_runbook_is_indexed_once(self) -> None:
        self.assertTrue(RUNBOOK.is_file())
        text = RUNBOOK.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# "))
        self.assertGreater(len(text.strip()), 500)

        index = INDEX.read_text(encoding="utf-8")
        self.assertEqual(index.count("](host-boot-generation-monitoring.md)"), 1)

    def test_runbook_keeps_targeted_read_only_contract(self) -> None:
        text = RUNBOOK.read_text(encoding="utf-8")
        script = SCRIPT.read_text(encoding="utf-8")
        host_aggregate = HOST_AGGREGATE.read_text(encoding="utf-8")

        self.assertIn("boot_generation_changed", text)
        self.assertIn("baseline は checker が作成・更新しない", text)
        self.assertIn("既定では `check-host-pressure.sh` aggregate へ自動追加しない", text)
        self.assertIn("IRLIGHT_HOST_BOOT_GENERATION_MODE=enabled", text)
        self.assertIn("status=WARNING reason=boot_generation_changed", script)
        self.assertIn(
            'boot_generation_mode="${IRLIGHT_HOST_BOOT_GENERATION_MODE:-disabled}"',
            host_aggregate,
        )
        self.assertIn('add_component "boot_generation"', host_aggregate)
        self.assertIn("check-host-boot-generation.sh", host_aggregate)


if __name__ == "__main__":
    unittest.main()
