from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AGGREGATE = ROOT / "scripts" / "check-host-pressure.sh"
RUNBOOK = ROOT / "docs" / "operations" / "host-pressure-monitoring.md"
INDEX = ROOT / "docs" / "operations" / "README.md"


class HostPressureOptInMatrixTests(unittest.TestCase):
    def _matrix(self) -> str:
        text = RUNBOOK.read_text(encoding="utf-8")
        start = text.index("## Optional host signals")
        end = text.index("## Targeted cgroup v2 PID check", start)
        return text[start:end]

    def test_matrix_tracks_every_host_opt_in_mode_from_script(self) -> None:
        script = AGGREGATE.read_text(encoding="utf-8")
        matrix = self._matrix()

        script_modes = set(
            re.findall(r"(IRLIGHT_HOST_[A-Z0-9_]+_MODE):-disabled", script)
        )
        documented_modes = set(
            re.findall(r"`(IRLIGHT_HOST_[A-Z0-9_]+_MODE)`", matrix)
        )

        self.assertEqual(documented_modes, script_modes)

    def test_matrix_documents_component_fields_paths_and_runbooks(self) -> None:
        matrix = self._matrix()
        contracts = (
            (
                "IRLIGHT_HOST_SWAP_PRESSURE_MODE",
                "swap_pressure_status",
                "IRLIGHT_SWAP_MEMINFO_PATH",
                None,
                "host-swap-pressure-monitoring.md",
            ),
            (
                "IRLIGHT_HOST_SWAP_IO_MODE",
                "swap_io_status",
                "IRLIGHT_VMSTAT_PATH",
                "IRLIGHT_VMSTAT_BASELINE_PATH",
                "host-swap-pressure-monitoring.md",
            ),
            (
                "IRLIGHT_HOST_OOM_KILL_MODE",
                "oom_kill_status",
                "IRLIGHT_VMSTAT_PATH",
                "IRLIGHT_VMSTAT_BASELINE_PATH",
                "oom-kill-monitoring.md",
            ),
            (
                "IRLIGHT_HOST_CPU_STEAL_MODE",
                "cpu_steal_status",
                "IRLIGHT_PROC_STAT_PATH",
                "IRLIGHT_PROC_STAT_BASELINE_PATH",
                "host-cpu-steal-monitoring.md",
            ),
            (
                "IRLIGHT_HOST_BOOT_GENERATION_MODE",
                "boot_generation_status",
                "IRLIGHT_BOOT_ID_PATH",
                "IRLIGHT_BOOT_ID_BASELINE_PATH",
                "host-boot-generation-monitoring.md",
            ),
            (
                "IRLIGHT_HOST_SOFTNET_MODE",
                "softnet_status",
                "IRLIGHT_SOFTNET_STAT_PATH",
                "IRLIGHT_SOFTNET_STAT_BASELINE_PATH",
                "softnet-pressure-monitoring.md",
            ),
            (
                "IRLIGHT_HOST_CLOCK_SYNC_MODE",
                "clock_sync_status",
                "IRLIGHT_TIMEDATECTL_BIN",
                None,
                "host-clock-sync-monitoring.md",
            ),
            (
                "IRLIGHT_HOST_NETWORK_LINK_MODE",
                "network_link_status",
                "IRLIGHT_NETWORK_INTERFACE_DIR",
                None,
                "host-network-link-monitoring.md",
            ),
            (
                "IRLIGHT_HOST_FILESYSTEM_READONLY_MODE",
                "filesystem_readonly_status",
                "IRLIGHT_FILESYSTEM_PATH",
                None,
                "filesystem-readonly-monitoring.md",
            ),
            (
                "IRLIGHT_HOST_FILESYSTEM_MOUNTPOINT_MODE",
                "filesystem_mountpoint_status",
                "IRLIGHT_EXPECTED_MOUNTPOINT_PATH",
                None,
                "filesystem-mountpoint-monitoring.md",
            ),
        )

        for mode, field, current_path, baseline_path, runbook in contracts:
            with self.subTest(mode=mode):
                row = next(
                    line
                    for line in matrix.splitlines()
                    if line.startswith("|") and f"`{mode}`" in line
                )
                self.assertIn(f"`{field}`", row)
                self.assertIn(f"`{current_path}`", row)
                if baseline_path is not None:
                    self.assertIn(f"`{baseline_path}`", row)
                self.assertIn(f"]({runbook})", row)

    def test_matrix_keeps_fail_closed_and_operator_owned_baseline_contract(self) -> None:
        matrix = self._matrix()
        self.assertIn("すべて `disabled`", matrix)
        self.assertIn("`enabled` / `disabled` のみ", matrix)
        self.assertIn("`CRITICAL > UNKNOWN > WARNING > OK`", matrix)
        self.assertIn("aggregate 自体を `UNKNOWN` に fail-closed", matrix)
        self.assertIn("aggregate は baseline を作成・更新・削除しない", matrix)
        self.assertIn("host-boot-generation-monitoring.md", matrix)
        self.assertIn("自動実行しない", matrix)

    def test_operations_index_links_host_pressure_matrix(self) -> None:
        index = INDEX.read_text(encoding="utf-8")
        self.assertIn(
            "[host-pressure-monitoring.md](host-pressure-monitoring.md)",
            index,
        )
        self.assertIn("opt-in component matrix", index)


if __name__ == "__main__":
    unittest.main()
