from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-host-swap-pressure.sh"


class HostSwapPressureCheckTest(unittest.TestCase):
    def _run(
        self,
        *,
        total_kb: str = "1000",
        free_kb: str = "500",
        warning: str = "80",
        critical: str = "90",
        total_unit: str = "kB",
        free_unit: str = "kB",
        include_total: bool = True,
        include_free: bool = True,
        duplicate_total: bool = False,
        duplicate_free: bool = False,
        path_exists: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="irlight-host-swap-check-") as temporary:
            root = Path(temporary)
            meminfo = root / "meminfo"
            if path_exists:
                lines: list[str] = []
                if include_total:
                    lines.append(f"SwapTotal: {total_kb} {total_unit}")
                    if duplicate_total:
                        lines.append(f"SwapTotal: {total_kb} {total_unit}")
                if include_free:
                    lines.append(f"SwapFree: {free_kb} {free_unit}")
                    if duplicate_free:
                        lines.append(f"SwapFree: {free_kb} {free_unit}")
                meminfo.write_text("\n".join(lines) + "\n", encoding="utf-8")

            env = os.environ.copy()
            env["IRLIGHT_SWAP_WARNING_PERCENT"] = warning
            env["IRLIGHT_SWAP_CRITICAL_PERCENT"] = critical
            return subprocess.run(
                ["bash", str(SCRIPT), str(meminfo)],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

    def test_no_configured_swap_is_ok(self) -> None:
        result = self._run(total_kb="0", free_kb="0")
        self.assertEqual(result.returncode, 0)
        self.assertIn("status=OK", result.stdout)
        self.assertIn("usage_percent=NA", result.stdout)
        self.assertIn("free_kb=0", result.stdout)
        self.assertIn("total_kb=0", result.stdout)

    def test_ok_below_warning_threshold(self) -> None:
        result = self._run(free_kb="210")
        self.assertEqual(result.returncode, 0)
        self.assertIn("status=OK", result.stdout)
        self.assertIn("usage_percent=79", result.stdout)

    def test_warning_at_warning_threshold(self) -> None:
        result = self._run(free_kb="200")
        self.assertEqual(result.returncode, 1)
        self.assertIn("status=WARNING", result.stdout)
        self.assertIn("usage_percent=80", result.stdout)

    def test_critical_at_critical_threshold(self) -> None:
        result = self._run(free_kb="100")
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("usage_percent=90", result.stdout)

    def test_zero_free_swap_is_critical(self) -> None:
        result = self._run(free_kb="0")
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("usage_percent=100", result.stdout)

    def test_thresholds_are_bounded_decimal_values(self) -> None:
        result = self._run(free_kb="200", warning="080", critical="090")
        self.assertEqual(result.returncode, 1)
        self.assertIn("warning_percent=80", result.stdout)
        self.assertIn("critical_percent=90", result.stdout)

    def test_invalid_thresholds_fail_closed(self) -> None:
        cases = (
            {"warning": "90", "critical": "80"},
            {"warning": "80x", "critical": "90"},
            {"warning": "9999", "critical": "10000"},
            {"warning": "101", "critical": "102"},
        )
        for kwargs in cases:
            with self.subTest(**kwargs):
                result = self._run(**kwargs)
                self.assertEqual(result.returncode, 3)
                self.assertEqual(
                    result.stdout.strip(),
                    "IRLIGHT_HOST_SWAP_PRESSURE status=UNKNOWN reason=invalid_threshold",
                )

    def test_missing_meminfo_is_unknown(self) -> None:
        result = self._run(path_exists=False)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_HOST_SWAP_PRESSURE status=UNKNOWN reason=meminfo_unavailable",
        )

    def test_missing_required_metric_is_unknown(self) -> None:
        result = self._run(include_free=False)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_HOST_SWAP_PRESSURE status=UNKNOWN reason=invalid_meminfo",
        )

    def test_invalid_or_inconsistent_meminfo_is_unknown(self) -> None:
        cases = (
            {"total_kb": "not-a-number"},
            {"free_kb": "not-a-number"},
            {"total_kb": "0", "free_kb": "1"},
            {"total_kb": "1000", "free_kb": "1001"},
            {"total_kb": "9223372036854775808", "free_kb": "1"},
            {"total_unit": "bytes"},
            {"free_unit": "bytes"},
            {"duplicate_total": True},
            {"duplicate_free": True},
        )
        for kwargs in cases:
            with self.subTest(**kwargs):
                result = self._run(**kwargs)
                self.assertEqual(result.returncode, 3)
                self.assertEqual(
                    result.stdout.strip(),
                    "IRLIGHT_HOST_SWAP_PRESSURE status=UNKNOWN reason=invalid_meminfo",
                )

    def test_large_valid_values_do_not_overflow_percentage_math(self) -> None:
        result = self._run(
            total_kb="9223372036854775807",
            free_kb="1844674407370955162",
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("status=OK", result.stdout)
        self.assertIn("usage_percent=79", result.stdout)


if __name__ == "__main__":
    unittest.main()
