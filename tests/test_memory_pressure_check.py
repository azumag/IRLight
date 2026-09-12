from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-memory-pressure.sh"


class MemoryPressureCheckTest(unittest.TestCase):
    def _run(
        self,
        *,
        total_kb: str = "1000",
        available_kb: str = "500",
        warning: str = "80",
        critical: str = "90",
        total_unit: str = "kB",
        available_unit: str = "kB",
        include_total: bool = True,
        include_available: bool = True,
        duplicate_total: bool = False,
        duplicate_available: bool = False,
        path_exists: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="irlight-memory-check-") as temporary:
            root = Path(temporary)
            meminfo = root / "meminfo"
            if path_exists:
                lines: list[str] = []
                if include_total:
                    lines.append(f"MemTotal: {total_kb} {total_unit}")
                    if duplicate_total:
                        lines.append(f"MemTotal: {total_kb} {total_unit}")
                if include_available:
                    lines.append(f"MemAvailable: {available_kb} {available_unit}")
                    if duplicate_available:
                        lines.append(f"MemAvailable: {available_kb} {available_unit}")
                meminfo.write_text("\n".join(lines) + "\n", encoding="utf-8")

            env = os.environ.copy()
            env["IRLIGHT_MEMORY_WARNING_PERCENT"] = warning
            env["IRLIGHT_MEMORY_CRITICAL_PERCENT"] = critical
            return subprocess.run(
                ["bash", str(SCRIPT), str(meminfo)],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

    def test_ok_below_warning_threshold(self) -> None:
        result = self._run(available_kb="210")
        self.assertEqual(result.returncode, 0)
        self.assertIn("status=OK", result.stdout)
        self.assertIn("usage_percent=79", result.stdout)
        self.assertIn("available_kb=210", result.stdout)
        self.assertIn("total_kb=1000", result.stdout)

    def test_warning_at_warning_threshold(self) -> None:
        result = self._run(available_kb="200")
        self.assertEqual(result.returncode, 1)
        self.assertIn("status=WARNING", result.stdout)
        self.assertIn("usage_percent=80", result.stdout)

    def test_critical_at_critical_threshold(self) -> None:
        result = self._run(available_kb="100")
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("usage_percent=90", result.stdout)

    def test_thresholds_are_bounded_decimal_values(self) -> None:
        result = self._run(available_kb="200", warning="080", critical="090")
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
                    "IRLIGHT_MEMORY_PRESSURE status=UNKNOWN reason=invalid_threshold",
                )

    def test_missing_meminfo_is_unknown(self) -> None:
        result = self._run(path_exists=False)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_MEMORY_PRESSURE status=UNKNOWN reason=meminfo_unavailable",
        )

    def test_missing_required_metric_is_unknown(self) -> None:
        result = self._run(include_available=False)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_MEMORY_PRESSURE status=UNKNOWN reason=invalid_meminfo",
        )

    def test_invalid_or_inconsistent_meminfo_is_unknown(self) -> None:
        cases = (
            {"total_kb": "not-a-number"},
            {"available_kb": "not-a-number"},
            {"total_kb": "0", "available_kb": "0"},
            {"total_kb": "1000", "available_kb": "1001"},
            {"total_kb": "9999999999999999999", "available_kb": "1"},
            {"total_unit": "bytes"},
            {"available_unit": "bytes"},
            {"duplicate_total": True},
            {"duplicate_available": True},
        )
        for kwargs in cases:
            with self.subTest(**kwargs):
                result = self._run(**kwargs)
                self.assertEqual(result.returncode, 3)
                self.assertEqual(
                    result.stdout.strip(),
                    "IRLIGHT_MEMORY_PRESSURE status=UNKNOWN reason=invalid_meminfo",
                )


if __name__ == "__main__":
    unittest.main()
