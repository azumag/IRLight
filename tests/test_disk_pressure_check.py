from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-disk-pressure.sh"


class DiskPressureCheckTest(unittest.TestCase):
    def _run(
        self,
        *,
        usage: int = 50,
        available: int = 500,
        warning: str = "80",
        critical: str = "90",
        df_exit: int = 0,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="irlight-disk-check-") as temporary:
            root = Path(temporary)
            target = root / "state"
            target.mkdir()
            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_df = fake_bin / "df"
            if df_exit == 0:
                fake_df.write_text(
                    "#!/usr/bin/env bash\n"
                    "cat <<'EOF'\n"
                    "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                    f"/dev/fake 1000 {1000 - available} {available} {usage}% /state\n"
                    "EOF\n",
                    encoding="utf-8",
                )
            else:
                fake_df.write_text(
                    "#!/usr/bin/env bash\n"
                    f"exit {df_exit}\n",
                    encoding="utf-8",
                )
            fake_df.chmod(0o755)

            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            env["IRLIGHT_DISK_WARNING_PERCENT"] = warning
            env["IRLIGHT_DISK_CRITICAL_PERCENT"] = critical
            return subprocess.run(
                ["bash", str(SCRIPT), str(target)],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

    def test_ok_below_warning_threshold(self) -> None:
        result = self._run(usage=79, available=210)
        self.assertEqual(result.returncode, 0)
        self.assertIn("status=OK", result.stdout)
        self.assertIn("usage_percent=79", result.stdout)

    def test_warning_at_warning_threshold(self) -> None:
        result = self._run(usage=80, available=200)
        self.assertEqual(result.returncode, 1)
        self.assertIn("status=WARNING", result.stdout)

    def test_critical_at_critical_threshold(self) -> None:
        result = self._run(usage=90, available=100)
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)

    def test_thresholds_are_parsed_as_bounded_decimal_values(self) -> None:
        result = self._run(usage=80, warning="080", critical="090")
        self.assertEqual(result.returncode, 1)
        self.assertIn("status=WARNING", result.stdout)
        self.assertIn("warning_percent=80", result.stdout)
        self.assertIn("critical_percent=90", result.stdout)

    def test_invalid_thresholds_fail_closed_before_df(self) -> None:
        for warning, critical in (
            ("90", "80"),
            ("80x", "90"),
            ("9999", "10000"),
        ):
            with self.subTest(warning=warning, critical=critical):
                result = self._run(warning=warning, critical=critical)
                self.assertEqual(result.returncode, 3)
                self.assertEqual(
                    result.stdout.strip(),
                    "IRLIGHT_DISK_PRESSURE status=UNKNOWN reason=invalid_threshold",
                )

    def test_df_failure_is_unknown_without_raw_error(self) -> None:
        result = self._run(df_exit=7)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_DISK_PRESSURE status=UNKNOWN reason=df_failed",
        )
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
