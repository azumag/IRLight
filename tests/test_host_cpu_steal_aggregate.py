from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-host-pressure.sh"


class HostCpuStealAggregateTest(unittest.TestCase):
    def _run(
        self,
        *,
        cpu_steal_mode: str | None = None,
        component_code: int = 0,
        cpu_steal_code: int = 0,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="irlight-host-cpu-steal-aggregate-") as temporary:
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

            cpu_steal = scripts / "check-cpu-steal-delta.sh"
            cpu_steal.write_text(
                "#!/usr/bin/env bash\n"
                "set -eu\n"
                '[[ "${1:-}" == "${IRLIGHT_TEST_PROC_STAT_PATH:-}" ]] || exit 3\n'
                '[[ "${2:-}" == "${IRLIGHT_TEST_BASELINE_PATH:-}" ]] || exit 3\n'
                'exit "${IRLIGHT_TEST_CPU_STEAL_CODE:-0}"\n',
                encoding="utf-8",
            )

            proc_stat = root / "proc-stat"
            proc_stat.write_text("cpu 1 2 3 4 5 6 7 8 0 0\n", encoding="utf-8")
            baseline = root / "baseline"
            baseline.write_text("cpu 1 2 3 4 5 6 7 8 0 0\n", encoding="utf-8")

            env = os.environ.copy()
            env["IRLIGHT_TEST_COMPONENT_CODE"] = str(component_code)
            env["IRLIGHT_TEST_CPU_STEAL_CODE"] = str(cpu_steal_code)
            env["IRLIGHT_TEST_PROC_STAT_PATH"] = str(proc_stat)
            env["IRLIGHT_TEST_BASELINE_PATH"] = str(baseline)
            env["IRLIGHT_PROC_STAT_PATH"] = str(proc_stat)
            env["IRLIGHT_PROC_STAT_BASELINE_PATH"] = str(baseline)
            env["IRLIGHT_HOST_SWAP_IO_MODE"] = "disabled"
            if cpu_steal_mode is not None:
                env["IRLIGHT_HOST_CPU_STEAL_MODE"] = cpu_steal_mode
            else:
                env.pop("IRLIGHT_HOST_CPU_STEAL_MODE", None)

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
        self.assertNotIn("cpu_steal_status", result.stdout)

    def test_enabled_cpu_steal_warning_is_aggregated(self) -> None:
        result = self._run(cpu_steal_mode="enabled", cpu_steal_code=1)
        self.assertEqual(result.returncode, 1)
        self.assertIn("status=WARNING", result.stdout)
        self.assertIn("cpu_steal_status=WARNING", result.stdout)

    def test_enabled_cpu_steal_unknown_is_fail_closed(self) -> None:
        result = self._run(cpu_steal_mode="enabled", cpu_steal_code=3)
        self.assertEqual(result.returncode, 3)
        self.assertIn("status=UNKNOWN", result.stdout)
        self.assertIn("cpu_steal_status=UNKNOWN", result.stdout)

    def test_known_critical_still_wins_over_cpu_steal_unknown(self) -> None:
        result = self._run(cpu_steal_mode="enabled", component_code=2, cpu_steal_code=3)
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("cpu_steal_status=UNKNOWN", result.stdout)

    def test_invalid_cpu_steal_mode_fails_closed_before_components(self) -> None:
        result = self._run(cpu_steal_mode="sometimes")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_HOST_PRESSURE status=UNKNOWN reason=invalid_cpu_steal_mode",
        )

    def test_enabled_mode_forwards_proc_stat_and_baseline_paths(self) -> None:
        result = self._run(cpu_steal_mode="enabled", cpu_steal_code=0)
        self.assertEqual(result.returncode, 0)
        self.assertIn("cpu_steal_status=OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
