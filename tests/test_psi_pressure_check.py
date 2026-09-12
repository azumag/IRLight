from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-psi-pressure.sh"


class PsiPressureCheckTest(unittest.TestCase):
    def _run(
        self,
        *,
        cpu_some: str = "1.00",
        memory_some: str = "1.00",
        memory_full: str = "0.00",
        io_some: str = "1.00",
        io_full: str = "0.00",
        cpu_text: str | None = None,
        env_overrides: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="irlight-psi-pressure-") as temporary:
            psi_dir = Path(temporary)
            (psi_dir / "cpu").write_text(
                cpu_text
                if cpu_text is not None
                else f"some avg10={cpu_some} avg60=0.50 avg300=0.25 total=12345\n",
                encoding="utf-8",
            )
            (psi_dir / "memory").write_text(
                f"some avg10={memory_some} avg60=0.50 avg300=0.25 total=12345\n"
                f"full avg10={memory_full} avg60=0.10 avg300=0.05 total=1234\n",
                encoding="utf-8",
            )
            (psi_dir / "io").write_text(
                f"some avg10={io_some} avg60=0.50 avg300=0.25 total=12345\n"
                f"full avg10={io_full} avg60=0.10 avg300=0.05 total=1234\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            if env_overrides:
                env.update(env_overrides)
            return subprocess.run(
                ["bash", str(SCRIPT), str(psi_dir)],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

    def test_ok_below_default_thresholds(self) -> None:
        result = self._run()
        self.assertEqual(result.returncode, 0)
        self.assertIn("IRLIGHT_PSI_PRESSURE status=OK", result.stdout)
        self.assertIn("cpu_some_avg10=1.00", result.stdout)

    def test_some_warning_boundary(self) -> None:
        result = self._run(cpu_some="25.00")
        self.assertEqual(result.returncode, 1)
        self.assertIn("status=WARNING", result.stdout)

    def test_some_critical_boundary(self) -> None:
        result = self._run(io_some="50.00")
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)

    def test_full_warning_boundary(self) -> None:
        result = self._run(io_full="5.00")
        self.assertEqual(result.returncode, 1)
        self.assertIn("status=WARNING", result.stdout)

    def test_full_critical_boundary(self) -> None:
        result = self._run(memory_full="20.00")
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)

    def test_thresholds_are_decimal_and_configurable(self) -> None:
        result = self._run(
            cpu_some="9.50",
            env_overrides={
                "IRLIGHT_PSI_SOME_WARNING_PERCENT": "010",
                "IRLIGHT_PSI_SOME_CRITICAL_PERCENT": "020",
                "IRLIGHT_PSI_FULL_WARNING_PERCENT": "001",
                "IRLIGHT_PSI_FULL_CRITICAL_PERCENT": "010",
            },
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("some_warning_percent=10", result.stdout)
        self.assertIn("full_warning_percent=1", result.stdout)

    def test_invalid_threshold_is_unknown(self) -> None:
        result = self._run(
            env_overrides={"IRLIGHT_PSI_SOME_WARNING_PERCENT": "101"},
        )
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_PSI_PRESSURE status=UNKNOWN reason=invalid_threshold",
        )

    def test_malformed_cpu_psi_is_unknown(self) -> None:
        result = self._run(cpu_text="some avg10=NaN avg60=0.00 avg300=0.00 total=1\n")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_PSI_PRESSURE status=UNKNOWN reason=invalid_cpu_psi",
        )

    def test_malformed_unused_rolling_average_is_unknown(self) -> None:
        result = self._run(cpu_text="some avg10=1.00 avg60=NaN avg300=0.00 total=1\n")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_PSI_PRESSURE status=UNKNOWN reason=invalid_cpu_psi",
        )

    def test_out_of_range_percentage_is_unknown(self) -> None:
        result = self._run(memory_some="100.01")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_PSI_PRESSURE status=UNKNOWN reason=invalid_memory_psi",
        )

    def test_duplicate_some_line_is_unknown(self) -> None:
        result = self._run(
            cpu_text=(
                "some avg10=1.00 avg60=0.00 avg300=0.00 total=1\n"
                "some avg10=2.00 avg60=0.00 avg300=0.00 total=2\n"
            )
        )
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_PSI_PRESSURE status=UNKNOWN reason=invalid_cpu_psi",
        )


if __name__ == "__main__":
    unittest.main()
