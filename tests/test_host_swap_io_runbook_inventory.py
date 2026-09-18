from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "docs" / "operations" / "host-swap-pressure-monitoring.md"
SCRIPT = ROOT / "scripts" / "check-swap-io-delta.sh"
HOST_AGGREGATE = ROOT / "scripts" / "check-host-pressure.sh"


class HostSwapIoRunbookInventoryTest(unittest.TestCase):
    def test_runbook_captures_swap_io_contract_and_safety_boundary(self) -> None:
        text = RUNBOOK.read_text(encoding="utf-8")
        self.assertTrue(SCRIPT.is_file())
        self.assertIn("scripts/check-swap-io-delta.sh", text)
        self.assertIn("pswpin", text)
        self.assertIn("pswpout", text)
        self.assertIn("swap_io_activity", text)
        self.assertIn("counter_reset", text)
        self.assertIn("operator-managed baseline", text)
        self.assertIn("baseline を作成・更新しない", text)

    def test_swap_io_check_remains_explicit_opt_in(self) -> None:
        aggregate = HOST_AGGREGATE.read_text(encoding="utf-8")
        self.assertIn(
            'swap_io_mode="${IRLIGHT_HOST_SWAP_IO_MODE:-disabled}"',
            aggregate,
        )
        self.assertIn(
            'if [[ "$swap_io_mode" == "enabled" ]]; then\n'
            '  add_component "swap_io" "$script_dir/check-swap-io-delta.sh" '
            '"$vmstat_path" "$vmstat_baseline_path"\n'
            'fi',
            aggregate,
        )


if __name__ == "__main__":
    unittest.main()
