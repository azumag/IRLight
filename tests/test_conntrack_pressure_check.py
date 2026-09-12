from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-conntrack-pressure.sh"


class ConntrackPressureCheckTest(unittest.TestCase):
    def _run(
        self,
        count: str = "100\n",
        maximum: str = "1000\n",
        *,
        env_overrides: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="irlight-conntrack-pressure-") as temporary:
            root = Path(temporary)
            count_path = root / "nf_conntrack_count"
            max_path = root / "nf_conntrack_max"
            count_path.write_text(count, encoding="utf-8")
            max_path.write_text(maximum, encoding="utf-8")
            env = os.environ.copy()
            if env_overrides:
                env.update(env_overrides)
            return subprocess.run(
                ["bash", str(SCRIPT), str(count_path), str(max_path)],
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

    def test_warning_boundary(self) -> None:
        result = self._run("800\n", "1000\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("status=WARNING", result.stdout)

    def test_critical_boundary(self) -> None:
        result = self._run("900\n", "1000\n")
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)

    def test_thresholds_are_decimal_and_configurable(self) -> None:
        result = self._run(
            "85\n",
            "100\n",
            env_overrides={
                "IRLIGHT_CONNTRACK_WARNING_PERCENT": "080",
                "IRLIGHT_CONNTRACK_CRITICAL_PERCENT": "090",
            },
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("warning_percent=80", result.stdout)

    def test_leading_zero_values_are_decimal(self) -> None:
        result = self._run("0080\n", "0100\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("count=80", result.stdout)
        self.assertIn("maximum=100", result.stdout)

    def test_linux_signed_long_maximum_is_supported(self) -> None:
        result = self._run("1\n", "9223372036854775807\n")
        self.assertEqual(result.returncode, 0)
        self.assertIn("maximum=9223372036854775807", result.stdout)

    def test_large_values_do_not_round_up_across_thresholds(self) -> None:
        maximum = "9223372036854775807\n"

        result = self._run("7378697629483820645\n", maximum)
        self.assertEqual(result.returncode, 0)
        self.assertIn("status=OK", result.stdout)
        self.assertIn("usage_percent=79", result.stdout)

        result = self._run("8301034833169298226\n", maximum)
        self.assertEqual(result.returncode, 1)
        self.assertIn("status=WARNING", result.stdout)
        self.assertIn("usage_percent=89", result.stdout)

    def test_invalid_threshold_is_unknown(self) -> None:
        result = self._run(
            env_overrides={"IRLIGHT_CONNTRACK_WARNING_PERCENT": "101"},
        )
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_CONNTRACK_PRESSURE status=UNKNOWN reason=invalid_threshold",
        )

    def test_malformed_count_is_unknown(self) -> None:
        for content in (
            "",
            "one\n",
            "100 200\n",
            "100\n200\n",
            "9223372036854775808\n",
        ):
            with self.subTest(content=content):
                result = self._run(content, "1000\n")
                self.assertEqual(result.returncode, 3)
                self.assertEqual(
                    result.stdout.strip(),
                    "IRLIGHT_CONNTRACK_PRESSURE status=UNKNOWN reason=invalid_conntrack_count",
                )

    def test_malformed_maximum_is_unknown(self) -> None:
        for content in (
            "",
            "one\n",
            "100 200\n",
            "100\n200\n",
            "9223372036854775808\n",
        ):
            with self.subTest(content=content):
                result = self._run("100\n", content)
                self.assertEqual(result.returncode, 3)
                self.assertEqual(
                    result.stdout.strip(),
                    "IRLIGHT_CONNTRACK_PRESSURE status=UNKNOWN reason=invalid_conntrack_max",
                )

    def test_inconsistent_values_are_unknown(self) -> None:
        for count, maximum in (("101\n", "100\n"), ("0\n", "0\n")):
            with self.subTest(count=count, maximum=maximum):
                result = self._run(count, maximum)
                self.assertEqual(result.returncode, 3)
                self.assertEqual(
                    result.stdout.strip(),
                    "IRLIGHT_CONNTRACK_PRESSURE status=UNKNOWN reason=invalid_conntrack_values",
                )


if __name__ == "__main__":
    unittest.main()
