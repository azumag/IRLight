from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-cpu-steal-delta.sh"


def cpu_line(
    user: int,
    nice: int,
    system: int,
    idle: int,
    iowait: int,
    irq: int,
    softirq: int,
    steal: int,
    *extra: int,
) -> str:
    fields = [user, nice, system, idle, iowait, irq, softirq, steal, *extra]
    return "cpu  " + " ".join(str(value) for value in fields) + "\n"


class CpuStealDeltaCheckTests(unittest.TestCase):
    def run_check(
        self,
        current: str | None,
        baseline: str | None,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="irlight-cpu-steal-") as temporary:
            root = Path(temporary)
            current_path = root / "current.stat"
            baseline_path = root / "baseline.stat"
            if current is not None:
                current_path.write_text(current, encoding="ascii")
            if baseline is not None:
                baseline_path.write_text(baseline, encoding="ascii")

            env = dict(os.environ)
            env.pop("IRLIGHT_PROC_STAT_PATH", None)
            env.pop("IRLIGHT_PROC_STAT_BASELINE_PATH", None)
            return subprocess.run(
                ["bash", str(SCRIPT), str(current_path), str(baseline_path)],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )

    def test_no_steal_delta_is_ok(self) -> None:
        baseline = cpu_line(100, 1, 20, 1000, 2, 3, 4, 5)
        current = cpu_line(120, 1, 25, 1100, 3, 4, 6, 5)
        result = self.run_check(current, baseline)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            "IRLIGHT_CPU_STEAL status=OK reason=none steal_ticks_delta=0\n",
        )

    def test_steal_delta_is_warning(self) -> None:
        baseline = cpu_line(100, 1, 20, 1000, 2, 3, 4, 5)
        current = cpu_line(120, 1, 25, 1100, 3, 4, 6, 9)
        result = self.run_check(current, baseline)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(
            result.stdout,
            "IRLIGHT_CPU_STEAL status=WARNING reason=cpu_steal_activity steal_ticks_delta=4\n",
        )

    def test_counter_reset_is_unknown(self) -> None:
        baseline = cpu_line(100, 1, 20, 1000, 2, 3, 4, 5)
        current = cpu_line(99, 1, 25, 1100, 3, 4, 6, 9)
        result = self.run_check(current, baseline)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(
            result.stdout,
            "IRLIGHT_CPU_STEAL status=UNKNOWN reason=counter_reset\n",
        )

    def test_per_cpu_lines_are_ignored(self) -> None:
        baseline = cpu_line(100, 1, 20, 1000, 2, 3, 4, 5) + cpu_line(
            10, 0, 2, 100, 0, 0, 0, 10
        ).replace("cpu  ", "cpu0 ", 1)
        current = cpu_line(120, 1, 25, 1100, 3, 4, 6, 5) + cpu_line(
            20, 0, 3, 110, 0, 0, 0, 99
        ).replace("cpu  ", "cpu0 ", 1)
        result = self.run_check(current, baseline)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("steal_ticks_delta=0", result.stdout)

    def test_future_extra_fields_are_accepted(self) -> None:
        baseline = cpu_line(100, 1, 20, 1000, 2, 3, 4, 5, 7, 8)
        current = cpu_line(120, 1, 25, 1100, 3, 4, 6, 5, 999, 1000)
        result = self.run_check(current, baseline)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_leading_zero_counters_are_supported(self) -> None:
        baseline = "cpu  0001 0000 0002 0010 0000 0000 0000 0003\n"
        current = "cpu  0002 0000 0002 0011 0000 0000 0000 0004\n"
        result = self.run_check(current, baseline)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("steal_ticks_delta=1", result.stdout)

    def test_malformed_aggregate_is_unknown(self) -> None:
        baseline = cpu_line(1, 0, 2, 10, 0, 0, 0, 3)
        malformed = (
            "cpu  1 0 2 10 0 0 0\n",
            "cpu  1 0 2 10 0 0 nope 3\n",
            "cpu  1 0 2 10 0 0 0 9223372036854775808\n",
            baseline + baseline,
        )
        for current in malformed:
            with self.subTest(current=current):
                result = self.run_check(current, baseline)
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertEqual(
                    result.stdout,
                    "IRLIGHT_CPU_STEAL status=UNKNOWN reason=invalid_cpu_stat_record\n",
                )

    def test_missing_aggregate_is_unknown(self) -> None:
        baseline = cpu_line(1, 0, 2, 10, 0, 0, 0, 3)
        result = self.run_check("intr 1 2 3\n", baseline)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(
            result.stdout,
            "IRLIGHT_CPU_STEAL status=UNKNOWN reason=cpu_aggregate_unavailable\n",
        )

    def test_missing_inputs_are_unknown_without_path_echo(self) -> None:
        result = self.run_check(None, cpu_line(1, 0, 2, 10, 0, 0, 0, 3))
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout,
            "IRLIGHT_CPU_STEAL status=UNKNOWN reason=current_proc_stat_unavailable\n",
        )
        self.assertNotIn("irlight-cpu-steal-", result.stdout + result.stderr)

        result = self.run_check(cpu_line(1, 0, 2, 10, 0, 0, 0, 3), None)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout,
            "IRLIGHT_CPU_STEAL status=UNKNOWN reason=baseline_proc_stat_unavailable\n",
        )
        self.assertNotIn("irlight-cpu-steal-", result.stdout + result.stderr)

    def test_environment_paths_are_supported(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-cpu-steal-") as temporary:
            root = Path(temporary)
            current_path = root / "current.stat"
            baseline_path = root / "baseline.stat"
            baseline_path.write_text(
                cpu_line(1, 0, 2, 10, 0, 0, 0, 3), encoding="ascii"
            )
            current_path.write_text(
                cpu_line(2, 0, 2, 11, 0, 0, 0, 4), encoding="ascii"
            )
            env = dict(os.environ)
            env["IRLIGHT_PROC_STAT_PATH"] = str(current_path)
            env["IRLIGHT_PROC_STAT_BASELINE_PATH"] = str(baseline_path)
            result = subprocess.run(
                ["bash", str(SCRIPT)],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("steal_ticks_delta=1", result.stdout)

    def test_missing_baseline_configuration_is_unknown(self) -> None:
        env = dict(os.environ)
        env.pop("IRLIGHT_PROC_STAT_BASELINE_PATH", None)
        with tempfile.TemporaryDirectory(prefix="irlight-cpu-steal-") as temporary:
            current_path = Path(temporary) / "current.stat"
            current_path.write_text(
                cpu_line(1, 0, 2, 10, 0, 0, 0, 3), encoding="ascii"
            )
            result = subprocess.run(
                ["bash", str(SCRIPT), str(current_path)],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout,
            "IRLIGHT_CPU_STEAL status=UNKNOWN reason=baseline_proc_stat_unavailable\n",
        )


if __name__ == "__main__":
    unittest.main()
