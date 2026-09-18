from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-host-pressure.sh"


class HostSwapIoAggregateTest(unittest.TestCase):
    def _run(
        self,
        *,
        swap_mode: str | None = None,
        component_code: int = 0,
        swap_code: int = 0,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="irlight-host-swap-io-aggregate-") as temporary:
            root = Path(temporary)
            scripts = root / "scripts"
            scripts.mkdir()
            shutil.copyfile(SCRIPT, scripts / "check-host-pressure.sh")

            component = """#!/usr/bin/env bash
exit "${IRLIGHT_TEST_COMPONENT_CODE:-0}"
"""
            for name in (
                "check-disk-pressure.sh",
                "check-memory-pressure.sh",
                "check-load-pressure.sh",
                "check-psi-pressure.sh",
                "check-file-handle-pressure.sh",
                "check-conntrack-pressure.sh",
                "check-task-pressure.sh",
            ):
                (scripts / name).write_text(component, encoding="utf-8")

            swap = scripts / "check-swap-io-delta.sh"
            swap.write_text(
                """#!/usr/bin/env bash
set -eu
[[ "${1:-}" == "${IRLIGHT_TEST_VMSTAT_PATH:-}" ]] || exit 3
[[ "${2:-}" == "${IRLIGHT_TEST_BASELINE_PATH:-}" ]] || exit 3
exit "${IRLIGHT_TEST_SWAP_CODE:-0}"
""",
                encoding="utf-8",
            )

            vmstat = root / "vmstat"
            vmstat.write_text("pswpin 1\npswpout 2\n", encoding="utf-8")
            baseline = root / "baseline"
            baseline.write_text("pswpin 1\npswpout 2\n", encoding="utf-8")

            env = os.environ.copy()
            env["IRLIGHT_TEST_COMPONENT_CODE"] = str(component_code)
            env["IRLIGHT_TEST_SWAP_CODE"] = str(swap_code)
            env["IRLIGHT_TEST_VMSTAT_PATH"] = str(vmstat)
            env["IRLIGHT_TEST_BASELINE_PATH"] = str(baseline)
            env["IRLIGHT_VMSTAT_PATH"] = str(vmstat)
            env["IRLIGHT_VMSTAT_BASELINE_PATH"] = str(baseline)
            if swap_mode is not None:
                env["IRLIGHT_HOST_SWAP_IO_MODE"] = swap_mode
            else:
                env.pop("IRLIGHT_HOST_SWAP_IO_MODE", None)

            return subprocess.run(
                [
                    "bash",
                    str(scripts / "check-host-pressure.sh"),
                    "disk",
                    "meminfo",
                    "loadavg",
                    "4",
                    "psi",
                    "file-nr",
                    "conntrack-count",
                    "conntrack-max",
                    "threads-max",
                ],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

    def test_default_output_contract_is_unchanged(self) -> None:
        result = self._run()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_HOST_PRESSURE status=OK disk_status=OK memory_status=OK load_status=OK psi_status=OK file_handle_status=OK conntrack_status=OK task_status=OK",
        )
        self.assertNotIn("swap_io_status", result.stdout)

    def test_enabled_swap_io_warning_is_aggregated(self) -> None:
        result = self._run(swap_mode="enabled", swap_code=1)
        self.assertEqual(result.returncode, 1)
        self.assertIn("status=WARNING", result.stdout)
        self.assertIn("swap_io_status=WARNING", result.stdout)

    def test_enabled_swap_io_unknown_is_fail_closed(self) -> None:
        result = self._run(swap_mode="enabled", swap_code=3)
        self.assertEqual(result.returncode, 3)
        self.assertIn("status=UNKNOWN", result.stdout)
        self.assertIn("swap_io_status=UNKNOWN", result.stdout)

    def test_known_critical_still_wins_over_swap_io_unknown(self) -> None:
        result = self._run(swap_mode="enabled", component_code=2, swap_code=3)
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("swap_io_status=UNKNOWN", result.stdout)

    def test_invalid_swap_mode_fails_closed_before_components(self) -> None:
        result = self._run(swap_mode="sometimes")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_HOST_PRESSURE status=UNKNOWN reason=invalid_swap_io_mode",
        )

    def test_enabled_mode_forwards_vmstat_and_baseline_paths(self) -> None:
        result = self._run(swap_mode="enabled", swap_code=0)
        self.assertEqual(result.returncode, 0)
        self.assertIn("swap_io_status=OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
