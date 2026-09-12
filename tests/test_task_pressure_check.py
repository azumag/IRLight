from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-task-pressure.sh"


class TaskPressureCheckTest(unittest.TestCase):
    def _run(
        self,
        loadavg: str = "0.10 0.20 0.30 1/100 123\n",
        threads_max: str = "1000\n",
        *,
        env_overrides: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="irlight-task-pressure-") as temporary:
            root = Path(temporary)
            loadavg_path = root / "loadavg"
            threads_max_path = root / "threads-max"
            loadavg_path.write_text(loadavg, encoding="utf-8")
            threads_max_path.write_text(threads_max, encoding="utf-8")
            env = os.environ.copy()
            if env_overrides:
                env.update(env_overrides)
            return subprocess.run(
                ["bash", str(SCRIPT), str(loadavg_path), str(threads_max_path)],
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
        self.assertIn("running=1", result.stdout)
        self.assertIn("total=100", result.stdout)

    def test_warning_boundary(self) -> None:
        result = self._run("0.10 0.20 0.30 2/800 123\n", "1000\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("status=WARNING", result.stdout)

    def test_critical_boundary(self) -> None:
        result = self._run("0.10 0.20 0.30 2/900 123\n", "1000\n")
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)

    def test_thresholds_are_decimal_and_configurable(self) -> None:
        result = self._run(
            "0.10 0.20 0.30 1/85 123\n",
            "100\n",
            env_overrides={
                "IRLIGHT_TASK_WARNING_PERCENT": "080",
                "IRLIGHT_TASK_CRITICAL_PERCENT": "090",
            },
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("warning_percent=80", result.stdout)

    def test_leading_zero_values_are_decimal(self) -> None:
        result = self._run("0.10 0.20 0.30 0002/0080 123\n", "0100\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("running=2", result.stdout)
        self.assertIn("total=80", result.stdout)
        self.assertIn("maximum=100", result.stdout)

    def test_linux_signed_long_maximum_is_supported(self) -> None:
        result = self._run(
            "0.10 0.20 0.30 1/1 123\n",
            "9223372036854775807\n",
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("maximum=9223372036854775807", result.stdout)

    def test_invalid_threshold_is_unknown(self) -> None:
        result = self._run(env_overrides={"IRLIGHT_TASK_WARNING_PERCENT": "101"})
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_TASK_PRESSURE status=UNKNOWN reason=invalid_threshold",
        )

    def test_malformed_loadavg_is_unknown(self) -> None:
        for content in (
            "",
            "0.10 0.20\n",
            "0.10 0.20 0.30 one 123\n",
            "0.10 0.20 0.30 1/100 123 extra\n",
            "0.10 0.20 0.30 2/1 123\n",
            "0.10 0.20 0.30 1/9223372036854775808 123\n",
            "0.10 0.20 0.30 1/100 123\n0.1 0.2 0.3 1/100 123\n",
        ):
            with self.subTest(content=content):
                result = self._run(content)
                self.assertEqual(result.returncode, 3)
                self.assertEqual(
                    result.stdout.strip(),
                    "IRLIGHT_TASK_PRESSURE status=UNKNOWN reason=invalid_loadavg",
                )

    def test_malformed_threads_max_is_unknown(self) -> None:
        for content in (
            "",
            "one\n",
            "100 200\n",
            "100\n200\n",
            "9223372036854775808\n",
        ):
            with self.subTest(content=content):
                result = self._run(threads_max=content)
                self.assertEqual(result.returncode, 3)
                self.assertEqual(
                    result.stdout.strip(),
                    "IRLIGHT_TASK_PRESSURE status=UNKNOWN reason=invalid_threads_max",
                )

    def test_inconsistent_values_are_unknown(self) -> None:
        for loadavg, maximum in (
            ("0.10 0.20 0.30 1/101 123\n", "100\n"),
            ("0.10 0.20 0.30 1/1 123\n", "0\n"),
        ):
            with self.subTest(loadavg=loadavg, maximum=maximum):
                result = self._run(loadavg, maximum)
                self.assertEqual(result.returncode, 3)
                self.assertEqual(
                    result.stdout.strip(),
                    "IRLIGHT_TASK_PRESSURE status=UNKNOWN reason=invalid_task_values",
                )


if __name__ == "__main__":
    unittest.main()
