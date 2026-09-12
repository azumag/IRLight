from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PID_CHECK = ROOT / "scripts" / "check-cgroup-pid-pressure.sh"
MEMORY_CHECK = ROOT / "scripts" / "check-cgroup-memory-pressure.sh"


class CgroupLimitPressureSharedContractTest(unittest.TestCase):
    def _run_pair(
        self,
        current: str,
        maximum: str,
        *,
        warning: str = "80",
        critical: str = "90",
    ) -> tuple[subprocess.CompletedProcess[str], subprocess.CompletedProcess[str]]:
        with tempfile.TemporaryDirectory(prefix="irlight-cgroup-limit-") as temporary:
            root = Path(temporary)
            current_path = root / "current"
            maximum_path = root / "maximum"
            current_path.write_text(current, encoding="utf-8")
            maximum_path.write_text(maximum, encoding="utf-8")

            base_env = os.environ.copy()
            pid_env = {
                **base_env,
                "IRLIGHT_CGROUP_PIDS_WARNING_PERCENT": warning,
                "IRLIGHT_CGROUP_PIDS_CRITICAL_PERCENT": critical,
            }
            memory_env = {
                **base_env,
                "IRLIGHT_CGROUP_MEMORY_WARNING_PERCENT": warning,
                "IRLIGHT_CGROUP_MEMORY_CRITICAL_PERCENT": critical,
            }
            pid = subprocess.run(
                ["bash", str(PID_CHECK), str(current_path), str(maximum_path)],
                check=False,
                capture_output=True,
                text=True,
                env=pid_env,
            )
            memory = subprocess.run(
                ["bash", str(MEMORY_CHECK), str(current_path), str(maximum_path)],
                check=False,
                capture_output=True,
                text=True,
                env=memory_env,
            )
            return pid, memory

    @staticmethod
    def _fields(output: str) -> dict[str, str]:
        return dict(token.split("=", 1) for token in output.strip().split()[1:])

    def _assert_same_pressure_contract(
        self,
        pid: subprocess.CompletedProcess[str],
        memory: subprocess.CompletedProcess[str],
    ) -> None:
        self.assertEqual(pid.returncode, memory.returncode)
        pid_fields = self._fields(pid.stdout)
        memory_fields = self._fields(memory.stdout)
        for field in ("status", "usage_percent", "warning_percent", "critical_percent"):
            self.assertEqual(pid_fields.get(field), memory_fields.get(field), field)

    def test_warning_threshold_and_leading_zero_normalization_match(self) -> None:
        pid, memory = self._run_pair("085\n", "100\n", warning="080", critical="090")
        self._assert_same_pressure_contract(pid, memory)
        self.assertEqual(pid.returncode, 1)
        self.assertEqual(self._fields(pid.stdout)["usage_percent"], "85")

    def test_unlimited_maximum_contract_matches(self) -> None:
        pid, memory = self._run_pair("42\n", "max\n")
        self._assert_same_pressure_contract(pid, memory)
        self.assertEqual(pid.returncode, 0)
        self.assertEqual(self._fields(pid.stdout)["usage_percent"], "NA")

    def test_zero_headroom_contract_matches(self) -> None:
        pid, memory = self._run_pair("0\n", "0\n")
        self._assert_same_pressure_contract(pid, memory)
        self.assertEqual(pid.returncode, 2)
        self.assertEqual(self._fields(pid.stdout)["usage_percent"], "NO_HEADROOM")

    def test_over_limit_contract_matches(self) -> None:
        pid, memory = self._run_pair("101\n", "100\n")
        self._assert_same_pressure_contract(pid, memory)
        self.assertEqual(pid.returncode, 2)
        self.assertEqual(self._fields(pid.stdout)["usage_percent"], "OVER_LIMIT")

    def test_invalid_thresholds_fail_closed_in_both_wrappers(self) -> None:
        pid, memory = self._run_pair("1\n", "100\n", warning="90", critical="90")
        self.assertEqual(pid.returncode, 3)
        self.assertEqual(memory.returncode, 3)
        self.assertIn("status=UNKNOWN", pid.stdout)
        self.assertIn("reason=invalid_threshold", pid.stdout)
        self.assertIn("status=UNKNOWN", memory.stdout)
        self.assertIn("reason=invalid_threshold", memory.stdout)


if __name__ == "__main__":
    unittest.main()
