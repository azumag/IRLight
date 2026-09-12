from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-load-pressure.sh"


class LoadPressureCheckTest(unittest.TestCase):
    def _run(
        self,
        *,
        loadavg: str = "0.10 0.20 0.30 1/100 123\n",
        cpu_count: str = "4",
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="irlight-load-pressure-") as temporary:
            loadavg_path = Path(temporary) / "loadavg"
            loadavg_path.write_text(loadavg, encoding="utf-8")

            env = os.environ.copy()
            if extra_env:
                env.update(extra_env)

            return subprocess.run(
                ["bash", str(SCRIPT), str(loadavg_path), cpu_count],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

    def test_ok_below_warning_threshold(self) -> None:
        result = self._run(loadavg="1.00 2.00 3.00 1/100 123\n")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_LOAD_PRESSURE status=OK load5=2.00 cpu_count=4 load_percent=50 warning_percent=100 critical_percent=200",
        )

    def test_warning_at_normalized_capacity(self) -> None:
        result = self._run(loadavg="1.00 4.00 3.00 1/100 123\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("status=WARNING", result.stdout)
        self.assertIn("load_percent=100", result.stdout)

    def test_critical_at_double_normalized_capacity(self) -> None:
        result = self._run(loadavg="1.00 8.00 3.00 1/100 123\n")
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("load_percent=200", result.stdout)

    def test_thresholds_are_independent_and_decimal(self) -> None:
        result = self._run(
            loadavg="1.00 4.00 3.00 1/100 123\n",
            extra_env={
                "IRLIGHT_LOAD_WARNING_PERCENT": "0125",
                "IRLIGHT_LOAD_CRITICAL_PERCENT": "0250",
            },
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("warning_percent=125", result.stdout)
        self.assertIn("critical_percent=250", result.stdout)

    def test_invalid_cpu_count_is_unknown(self) -> None:
        result = self._run(cpu_count="0")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_LOAD_PRESSURE status=UNKNOWN reason=invalid_cpu_count",
        )

    def test_non_finite_or_signed_load_is_unknown(self) -> None:
        for load5 in ("NaN", "Infinity", "-1.00", "+1.00", "1e3"):
            with self.subTest(load5=load5):
                result = self._run(loadavg=f"0.10 {load5} 0.30 1/100 123\n")
                self.assertEqual(result.returncode, 3)
                self.assertEqual(
                    result.stdout.strip(),
                    "IRLIGHT_LOAD_PRESSURE status=UNKNOWN reason=invalid_loadavg",
                )

    def test_missing_load_field_is_unknown(self) -> None:
        result = self._run(loadavg="0.10\n")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_LOAD_PRESSURE status=UNKNOWN reason=invalid_loadavg",
        )

    def test_invalid_threshold_order_is_unknown(self) -> None:
        result = self._run(
            extra_env={
                "IRLIGHT_LOAD_WARNING_PERCENT": "200",
                "IRLIGHT_LOAD_CRITICAL_PERCENT": "100",
            }
        )
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_LOAD_PRESSURE status=UNKNOWN reason=invalid_threshold",
        )


if __name__ == "__main__":
    unittest.main()
