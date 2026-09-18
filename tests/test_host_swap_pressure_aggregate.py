from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-host-pressure.sh"


class HostSwapPressureAggregateTest(unittest.TestCase):
    def _run(
        self,
        *,
        swap_pressure_mode: str | None = None,
        component_code: int = 0,
        swap_pressure_code: int = 0,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="irlight-host-swap-pressure-aggregate-") as temporary:
            root = Path(temporary)
            scripts = root / "scripts"
            scripts.mkdir()
            shutil.copyfile(SCRIPT, scripts / "check-host-pressure.sh")

            component = (
                "#!/usr/bin/env bash\n"
                'exit "${IRLIGHT_TEST_COMPONENT_CODE:-0}"\n'
            )
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

            swap_pressure = scripts / "check-host-swap-pressure.sh"
            swap_pressure.write_text(
                "#!/usr/bin/env bash\n"
                "set -eu\n"
                '[[ "${1:-}" == "${IRLIGHT_TEST_SWAP_MEMINFO_PATH:-}" ]] || exit 3\n'
                'exit "${IRLIGHT_TEST_SWAP_PRESSURE_CODE:-0}"\n',
                encoding="utf-8",
            )

            meminfo = root / "meminfo"
            meminfo.write_text(
                "MemTotal: 1000 kB\nMemAvailable: 900 kB\nSwapTotal: 1000 kB\nSwapFree: 1000 kB\n",
                encoding="utf-8",
            )
            swap_meminfo = root / "swap-meminfo"
            swap_meminfo.write_text(
                "SwapTotal: 1000 kB\nSwapFree: 1000 kB\n",
                encoding="utf-8",
            )

            env = os.environ.copy()
            env["IRLIGHT_TEST_COMPONENT_CODE"] = str(component_code)
            env["IRLIGHT_TEST_SWAP_PRESSURE_CODE"] = str(swap_pressure_code)
            env["IRLIGHT_TEST_SWAP_MEMINFO_PATH"] = str(swap_meminfo)
            env["IRLIGHT_SWAP_MEMINFO_PATH"] = str(swap_meminfo)
            env["IRLIGHT_HOST_SWAP_IO_MODE"] = "disabled"
            env["IRLIGHT_HOST_CPU_STEAL_MODE"] = "disabled"
            env["IRLIGHT_HOST_BOOT_GENERATION_MODE"] = "disabled"
            if swap_pressure_mode is not None:
                env["IRLIGHT_HOST_SWAP_PRESSURE_MODE"] = swap_pressure_mode
            else:
                env.pop("IRLIGHT_HOST_SWAP_PRESSURE_MODE", None)

            return subprocess.run(
                [
                    "bash",
                    str(scripts / "check-host-pressure.sh"),
                    "disk",
                    str(meminfo),
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
        self.assertNotIn("swap_pressure_status", result.stdout)

    def test_enabled_swap_pressure_warning_is_aggregated(self) -> None:
        result = self._run(swap_pressure_mode="enabled", swap_pressure_code=1)
        self.assertEqual(result.returncode, 1)
        self.assertIn("status=WARNING", result.stdout)
        self.assertIn("swap_pressure_status=WARNING", result.stdout)

    def test_enabled_swap_pressure_critical_is_aggregated(self) -> None:
        result = self._run(swap_pressure_mode="enabled", swap_pressure_code=2)
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("swap_pressure_status=CRITICAL", result.stdout)

    def test_enabled_swap_pressure_unknown_is_fail_closed(self) -> None:
        result = self._run(swap_pressure_mode="enabled", swap_pressure_code=3)
        self.assertEqual(result.returncode, 3)
        self.assertIn("status=UNKNOWN", result.stdout)
        self.assertIn("swap_pressure_status=UNKNOWN", result.stdout)

    def test_known_critical_still_wins_over_swap_pressure_unknown(self) -> None:
        result = self._run(
            swap_pressure_mode="enabled",
            component_code=2,
            swap_pressure_code=3,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("swap_pressure_status=UNKNOWN", result.stdout)

    def test_invalid_swap_pressure_mode_fails_closed_before_components(self) -> None:
        result = self._run(swap_pressure_mode="sometimes")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_HOST_PRESSURE status=UNKNOWN reason=invalid_swap_pressure_mode",
        )

    def test_enabled_mode_forwards_swap_meminfo_path(self) -> None:
        result = self._run(swap_pressure_mode="enabled", swap_pressure_code=0)
        self.assertEqual(result.returncode, 0)
        self.assertIn("swap_pressure_status=OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
