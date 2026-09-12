from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-cgroup-pid-pressure.sh"


class CgroupPidPressureCheckTest(unittest.TestCase):
    def _run(
        self,
        current: str = "100\n",
        maximum: str = "1000\n",
        *,
        env_overrides: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="irlight-cgroup-pid-pressure-") as temporary:
            root = Path(temporary)
            current_path = root / "pids.current"
            maximum_path = root / "pids.max"
            current_path.write_text(current, encoding="utf-8")
            maximum_path.write_text(maximum, encoding="utf-8")
            env = os.environ.copy()
            if env_overrides:
                env.update(env_overrides)
            return subprocess.run(
                ["bash", str(SCRIPT), str(current_path), str(maximum_path)],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

    def test_ok_below_default_thresholds(self) -> None:
        result = self._run()
        self.assertEqual(result.returncode, 0)
        self.assertIn("status=OK", result.stdout)
        self.assertIn("usage_percent=10", result.stdout)
        self.assertIn("current=100", result.stdout)
        self.assertIn("maximum=1000", result.stdout)

    def test_warning_boundary(self) -> None:
        result = self._run("800\n", "1000\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("status=WARNING", result.stdout)
        self.assertIn("usage_percent=80", result.stdout)

    def test_critical_boundary(self) -> None:
        result = self._run("900\n", "1000\n")
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("usage_percent=90", result.stdout)

    def test_over_limit_is_critical_not_corrupt_telemetry(self) -> None:
        result = self._run("101\n", "100\n")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_CGROUP_PID_PRESSURE status=CRITICAL usage_percent=OVER_LIMIT current=101 maximum=100 warning_percent=80 critical_percent=90",
        )

    def test_zero_limit_is_valid_and_critical(self) -> None:
        result = self._run("0\n", "0\n")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_CGROUP_PID_PRESSURE status=CRITICAL usage_percent=NO_HEADROOM current=0 maximum=0 warning_percent=80 critical_percent=90",
        )

    def test_unlimited_local_limit_is_ok(self) -> None:
        result = self._run("123\n", "max\n")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_CGROUP_PID_PRESSURE status=OK usage_percent=NA current=123 maximum=max warning_percent=80 critical_percent=90",
        )

    def test_thresholds_are_decimal_and_configurable(self) -> None:
        result = self._run(
            "85\n",
            "100\n",
            env_overrides={
                "IRLIGHT_CGROUP_PIDS_WARNING_PERCENT": "080",
                "IRLIGHT_CGROUP_PIDS_CRITICAL_PERCENT": "090",
            },
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("warning_percent=80", result.stdout)

    def test_leading_zero_values_are_decimal(self) -> None:
        result = self._run("0080\n", "0100\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("current=80", result.stdout)
        self.assertIn("maximum=100", result.stdout)

    def test_linux_signed_long_maximum_is_supported(self) -> None:
        result = self._run("1\n", "9223372036854775807\n")
        self.assertEqual(result.returncode, 0)
        self.assertIn("maximum=9223372036854775807", result.stdout)

    def test_invalid_threshold_is_unknown(self) -> None:
        result = self._run(
            env_overrides={"IRLIGHT_CGROUP_PIDS_WARNING_PERCENT": "101"}
        )
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_CGROUP_PID_PRESSURE status=UNKNOWN reason=invalid_threshold",
        )

    def test_malformed_current_is_unknown(self) -> None:
        for content in (
            "",
            "one\n",
            "100 200\n",
            "100\n200\n",
            "9223372036854775808\n",
        ):
            with self.subTest(content=content):
                result = self._run(current=content)
                self.assertEqual(result.returncode, 3)
                self.assertEqual(
                    result.stdout.strip(),
                    "IRLIGHT_CGROUP_PID_PRESSURE status=UNKNOWN reason=invalid_current",
                )

    def test_malformed_maximum_is_unknown(self) -> None:
        for content in (
            "",
            "MAX\n",
            "100 200\n",
            "100\n200\n",
            "9223372036854775808\n",
        ):
            with self.subTest(content=content):
                result = self._run(maximum=content)
                self.assertEqual(result.returncode, 3)
                self.assertEqual(
                    result.stdout.strip(),
                    "IRLIGHT_CGROUP_PID_PRESSURE status=UNKNOWN reason=invalid_maximum",
                )


if __name__ == "__main__":
    unittest.main()
