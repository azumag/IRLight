from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "lib" / "scalar-pressure-common.sh"


class ScalarPressureCommonTest(unittest.TestCase):
    def _usage_percent(
        self, current: str, maximum: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "bash",
                "-c",
                'source "$1"; pressure_usage_percent "$2" "$3" || exit $?; '
                'printf "%s\\n" "$PRESSURE_VALUE"',
                "scalar-pressure-test",
                str(HELPER),
                current,
                maximum,
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_signed_long_boundary_below_warning_does_not_round_up(self) -> None:
        maximum = "9223372036854775807"
        # ceil(maximum * 80 / 100) - 1: exact usage is still 79%.
        result = self._usage_percent("7378697629483820645", maximum)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "79")

    def test_signed_long_boundary_below_critical_does_not_round_up(self) -> None:
        maximum = "9223372036854775807"
        # ceil(maximum * 90 / 100) - 1: exact usage is still 89%.
        result = self._usage_percent("8301034833169298226", maximum)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "89")

    def test_exact_boundaries_and_full_usage(self) -> None:
        maximum = "9223372036854775807"
        cases = (
            ("7378697629483820646", "80"),
            ("8301034833169298227", "90"),
            (maximum, "100"),
        )
        for current, expected in cases:
            with self.subTest(current=current):
                result = self._usage_percent(current, maximum)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), expected)

    def test_invalid_ratio_is_rejected(self) -> None:
        result = self._usage_percent("101", "100")
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
