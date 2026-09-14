from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-cgroup-runtime-pressure.sh"


def cpu_stat(*, nr_periods: int = 100, nr_throttled: int = 10, throttled_usec: int | str = 5000) -> str:
    return (
        "usage_usec 100000\n"
        "user_usec 70000\n"
        "system_usec 30000\n"
        f"nr_periods {nr_periods}\n"
        f"nr_throttled {nr_throttled}\n"
        f"throttled_usec {throttled_usec}\n"
    )


class CgroupCpuThrottlingAggregateTest(unittest.TestCase):
    def _run(
        self,
        *,
        current_cpu_stat: str,
        baseline_cpu_stat: str,
        pids_current: str = "10",
        swap: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="irlight-cgroup-cpu-aggregate-") as temporary:
            root = Path(temporary)
            cgroup_dir = root / "cgroup"
            cgroup_dir.mkdir()

            (cgroup_dir / "memory.current").write_text("100\n", encoding="utf-8")
            (cgroup_dir / "memory.max").write_text("1000\n", encoding="utf-8")
            (cgroup_dir / "memory.high").write_text("800\n", encoding="utf-8")
            (cgroup_dir / "pids.current").write_text(f"{pids_current}\n", encoding="utf-8")
            (cgroup_dir / "pids.max").write_text("100\n", encoding="utf-8")
            (cgroup_dir / "cpu.pressure").write_text(
                "some avg10=1.00 avg60=0.50 avg300=0.25 total=12345\n",
                encoding="utf-8",
            )
            for resource in ("memory", "io"):
                (cgroup_dir / f"{resource}.pressure").write_text(
                    "some avg10=1.00 avg60=0.50 avg300=0.25 total=12345\n"
                    "full avg10=0.00 avg60=0.10 avg300=0.05 total=1234\n",
                    encoding="utf-8",
                )
            (cgroup_dir / "cpu.stat").write_text(current_cpu_stat, encoding="utf-8")

            if swap:
                (cgroup_dir / "memory.swap.current").write_text("0\n", encoding="utf-8")
                (cgroup_dir / "memory.swap.max").write_text("max\n", encoding="utf-8")

            baseline = root / "cpu.stat.baseline"
            baseline.write_text(baseline_cpu_stat, encoding="utf-8")

            env = os.environ.copy()
            env["IRLIGHT_CGROUP_CPU_STAT_BASELINE_PATH"] = str(baseline)

            command = ["bash", str(SCRIPT), str(cgroup_dir)]
            if swap:
                command.extend(["", "", "enabled"])
            return subprocess.run(
                command,
                env=env,
                text=True,
                capture_output=True,
                check=False,
                timeout=8,
            )

    def test_throttling_warning_is_included_when_baseline_is_explicit(self) -> None:
        result = self._run(
            current_cpu_stat=cpu_stat(nr_periods=101, nr_throttled=11, throttled_usec=6000),
            baseline_cpu_stat=cpu_stat(),
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("status=WARNING", result.stdout)
        self.assertIn("cpu_throttling_status=WARNING", result.stdout)
        self.assertNotIn("swap_status=", result.stdout)

    def test_cpu_counter_reset_fails_closed(self) -> None:
        result = self._run(
            current_cpu_stat=cpu_stat(nr_throttled=9),
            baseline_cpu_stat=cpu_stat(),
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("status=UNKNOWN", result.stdout)
        self.assertIn("cpu_throttling_status=UNKNOWN", result.stdout)

    def test_known_critical_still_wins_over_cpu_unknown(self) -> None:
        result = self._run(
            current_cpu_stat=cpu_stat(throttled_usec="broken"),
            baseline_cpu_stat=cpu_stat(),
            pids_current="100",
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("pids_status=CRITICAL", result.stdout)
        self.assertIn("cpu_throttling_status=UNKNOWN", result.stdout)

    def test_cpu_and_swap_optional_fields_can_coexist(self) -> None:
        result = self._run(
            current_cpu_stat=cpu_stat(),
            baseline_cpu_stat=cpu_stat(),
            swap=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("cpu_throttling_status=OK", result.stdout)
        self.assertIn("swap_status=OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
