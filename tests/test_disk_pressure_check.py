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
        inode_usage: int = 40,
        inode_available: int = 600,
        warning: str = "80",
        critical: str = "90",
        inode_warning: str | None = None,
        inode_critical: str | None = None,
        df_exit: int = 0,
        inode_df_exit: int = 0,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="irlight-disk-check-") as temporary:
            root = Path(temporary)
            target = root / "state"
            target.mkdir()
            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_df = fake_bin / "df"
            fake_df.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "if [[ \"${1:-}\" == *i* ]]; then\n"
                f"  if (( {inode_df_exit} != 0 )); then echo 'inode df detail' >&2; exit {inode_df_exit}; fi\n"
                "  cat <<'EOF'\n"
                "Filesystem Inodes IUsed IFree IUse% Mounted on\n"
                f"/dev/fake 1000 {1000 - inode_available} {inode_available} {inode_usage}% /state\n"
                "EOF\n"
                "else\n"
                f"  if (( {df_exit} != 0 )); then echo 'block df detail' >&2; exit {df_exit}; fi\n"
                "  cat <<'EOF'\n"
                "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                f"/dev/fake 1000 {1000 - available} {available} {usage}% /state\n"
                "EOF\n"
                "fi\n",
                encoding="utf-8",
            )
            fake_df.chmod(0o755)

            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            env["IRLIGHT_DISK_WARNING_PERCENT"] = warning
            env["IRLIGHT_DISK_CRITICAL_PERCENT"] = critical
            if inode_warning is not None:
                env["IRLIGHT_DISK_INODE_WARNING_PERCENT"] = inode_warning
            else:
                env.pop("IRLIGHT_DISK_INODE_WARNING_PERCENT", None)
            if inode_critical is not None:
                env["IRLIGHT_DISK_INODE_CRITICAL_PERCENT"] = inode_critical
            else:
                env.pop("IRLIGHT_DISK_INODE_CRITICAL_PERCENT", None)
            return subprocess.run(
                ["bash", str(SCRIPT), str(target)],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

    def test_ok_below_warning_thresholds(self) -> None:
        result = self._run(usage=79, available=210, inode_usage=79, inode_available=210)
        self.assertEqual(result.returncode, 0)
        self.assertIn("status=OK", result.stdout)
        self.assertIn("usage_percent=79", result.stdout)
        self.assertIn("inode_usage_percent=79", result.stdout)

    def test_warning_at_block_warning_threshold(self) -> None:
        result = self._run(usage=80, available=200)
        self.assertEqual(result.returncode, 1)
        self.assertIn("status=WARNING", result.stdout)

    def test_critical_at_block_critical_threshold(self) -> None:
        result = self._run(usage=90, available=100)
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)

    def test_inode_warning_affects_overall_status(self) -> None:
        result = self._run(usage=20, inode_usage=80, inode_available=200)
        self.assertEqual(result.returncode, 1)
        self.assertIn("status=WARNING", result.stdout)
        self.assertIn("inode_usage_percent=80", result.stdout)

    def test_inode_critical_takes_precedence_over_block_warning(self) -> None:
        result = self._run(usage=80, inode_usage=90, inode_available=100)
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)

    def test_inode_thresholds_can_be_overridden_independently(self) -> None:
        result = self._run(
            usage=20,
            inode_usage=70,
            inode_warning="070",
            inode_critical="085",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("inode_warning_percent=70", result.stdout)
        self.assertIn("inode_critical_percent=85", result.stdout)

    def test_thresholds_are_parsed_as_bounded_decimal_values(self) -> None:
        result = self._run(usage=80, warning="080", critical="090")
        self.assertEqual(result.returncode, 1)
        self.assertIn("status=WARNING", result.stdout)
        self.assertIn("warning_percent=80", result.stdout)
        self.assertIn("critical_percent=90", result.stdout)
        self.assertIn("inode_warning_percent=80", result.stdout)
        self.assertIn("inode_critical_percent=90", result.stdout)

    def test_invalid_thresholds_fail_closed_before_df(self) -> None:
        cases = (
            {"warning": "90", "critical": "80"},
            {"warning": "80x", "critical": "90"},
            {"warning": "9999", "critical": "10000"},
            {"inode_warning": "95", "inode_critical": "90"},
            {"inode_warning": "80x", "inode_critical": "90"},
        )
        for kwargs in cases:
            with self.subTest(**kwargs):
                result = self._run(**kwargs)
                self.assertEqual(result.returncode, 3)
                self.assertEqual(
                    result.stdout.strip(),
                    "IRLIGHT_DISK_PRESSURE status=UNKNOWN reason=invalid_threshold",
                )

    def test_block_df_failure_is_unknown_without_raw_error(self) -> None:
        result = self._run(df_exit=7)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_DISK_PRESSURE status=UNKNOWN reason=df_failed",
        )
        self.assertEqual(result.stderr, "")

    def test_inode_df_failure_is_unknown_without_raw_error(self) -> None:
        result = self._run(inode_df_exit=8)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_DISK_PRESSURE status=UNKNOWN reason=df_inode_failed",
        )
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
