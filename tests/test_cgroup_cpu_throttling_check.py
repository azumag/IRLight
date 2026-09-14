from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-cgroup-cpu-throttling.sh"


def cpu_stat(**overrides: int | str) -> str:
    values: dict[str, int | str] = {
        "usage_usec": 100000,
        "user_usec": 70000,
        "system_usec": 30000,
        "nr_periods": 100,
        "nr_throttled": 10,
        "throttled_usec": 5000,
    }
    values.update(overrides)
    return "".join(f"{key} {value}\n" for key, value in values.items())


class CgroupCpuThrottlingCheckTest(unittest.TestCase):
    def _run(
        self,
        current: str,
        baseline: str,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="irlight-cgroup-cpu-throttling-") as temporary:
            root = Path(temporary)
            current_path = root / "cpu.stat"
            baseline_path = root / "cpu.stat.baseline"
            current_path.write_text(current, encoding="utf-8")
            baseline_path.write_text(baseline, encoding="utf-8")
            return subprocess.run(
                ["bash", str(SCRIPT), str(current_path), str(baseline_path)],
                env=os.environ.copy(),
                text=True,
                capture_output=True,
                check=False,
            )

    def test_equal_counters_are_ok(self) -> None:
        value = cpu_stat()
        result = self._run(value, value)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_CGROUP_CPU_THROTTLING status=OK reason=none "
            "nr_periods_delta=0 nr_throttled_delta=0 throttled_usec_delta=0",
        )

    def test_new_throttled_period_is_warning(self) -> None:
        result = self._run(
            cpu_stat(nr_periods=101, nr_throttled=11, throttled_usec=5000),
            cpu_stat(),
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("status=WARNING", result.stdout)
        self.assertIn("reason=cpu_throttling_activity", result.stdout)
        self.assertIn("nr_periods_delta=1", result.stdout)
        self.assertIn("nr_throttled_delta=1", result.stdout)

    def test_new_throttled_time_is_warning(self) -> None:
        result = self._run(
            cpu_stat(nr_periods=101, nr_throttled=10, throttled_usec=6000),
            cpu_stat(),
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("status=WARNING", result.stdout)
        self.assertIn("throttled_usec_delta=1000", result.stdout)

    def test_unrelated_cpu_usage_growth_is_ok(self) -> None:
        result = self._run(
            cpu_stat(usage_usec=200000, user_usec=150000, system_usec=50000, nr_periods=101),
            cpu_stat(),
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("status=OK", result.stdout)
        self.assertIn("nr_periods_delta=1", result.stdout)

    def test_counter_reset_fails_closed(self) -> None:
        result = self._run(
            cpu_stat(nr_throttled=9),
            cpu_stat(),
        )
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_CGROUP_CPU_THROTTLING status=UNKNOWN reason=counter_reset",
        )

    def test_missing_required_counter_fails_closed(self) -> None:
        current = cpu_stat().replace("nr_throttled 10\n", "")
        result = self._run(current, cpu_stat())
        self.assertEqual(result.returncode, 3)
        self.assertIn("reason=missing_required_counter", result.stdout)

    def test_invalid_counter_fails_closed(self) -> None:
        result = self._run(
            cpu_stat(throttled_usec="broken"),
            cpu_stat(),
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("reason=invalid_cpu_stat_record", result.stdout)

    def test_duplicate_interpreted_counter_fails_closed(self) -> None:
        result = self._run(
            cpu_stat() + "nr_throttled 10\n",
            cpu_stat(),
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("reason=duplicate_cpu_stat_record", result.stdout)

    def test_future_numeric_counter_is_allowed(self) -> None:
        result = self._run(
            cpu_stat() + "future_counter 123\n",
            cpu_stat() + "future_counter 100\n",
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("status=OK", result.stdout)

    def test_future_counter_still_requires_valid_record_shape(self) -> None:
        result = self._run(
            cpu_stat() + "future_counter not-a-number\n",
            cpu_stat(),
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("reason=invalid_cpu_stat_record", result.stdout)

    def test_signed_64_bit_boundary_is_accepted(self) -> None:
        maximum = 9223372036854775807
        result = self._run(
            cpu_stat(nr_periods=maximum, nr_throttled=maximum, throttled_usec=maximum),
            cpu_stat(nr_periods=maximum, nr_throttled=maximum, throttled_usec=maximum),
        )
        self.assertEqual(result.returncode, 0)

    def test_value_above_signed_64_bit_boundary_is_rejected(self) -> None:
        result = self._run(
            cpu_stat(throttled_usec=9223372036854775808),
            cpu_stat(),
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("reason=invalid_cpu_stat_record", result.stdout)


if __name__ == "__main__":
    unittest.main()
