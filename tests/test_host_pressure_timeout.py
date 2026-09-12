from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-host-pressure.sh"


class HostPressureTimeoutTest(unittest.TestCase):
    def _run(self, *, timeout_seconds: str, df_delay_seconds: str = "0") -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="irlight-host-pressure-timeout-") as temporary:
            root = Path(temporary)
            state_dir = root / "state"
            state_dir.mkdir()

            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_df = fake_bin / "df"
            fake_df.write_text(
                """#!/usr/bin/env bash
set -eu
sleep "${IRLIGHT_TEST_DF_DELAY_SECONDS:-0}"
case "${1:-}" in
  -P)
    printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\\n'
    printf 'fake 1000 100 900 10%% /fake\\n'
    ;;
  -Pi)
    printf 'Filesystem Inodes IUsed IFree IUse%% Mounted on\\n'
    printf 'fake 1000 100 900 10%% /fake\\n'
    ;;
  *)
    exit 64
    ;;
esac
""",
                encoding="utf-8",
            )
            fake_df.chmod(fake_df.stat().st_mode | stat.S_IXUSR)

            meminfo = root / "meminfo"
            meminfo.write_text("MemTotal: 1000 kB\nMemAvailable: 900 kB\n", encoding="utf-8")
            loadavg = root / "loadavg"
            loadavg.write_text("0.10 0.20 0.30 1/100 123\n", encoding="utf-8")

            psi_dir = root / "pressure"
            psi_dir.mkdir()
            (psi_dir / "cpu").write_text(
                "some avg10=1.00 avg60=0.50 avg300=0.25 total=100\n",
                encoding="utf-8",
            )
            for resource in ("memory", "io"):
                (psi_dir / resource).write_text(
                    "some avg10=1.00 avg60=0.50 avg300=0.25 total=100\n"
                    "full avg10=0.00 avg60=0.10 avg300=0.05 total=10\n",
                    encoding="utf-8",
                )

            file_nr = root / "file-nr"
            file_nr.write_text("100 0 1000\n", encoding="utf-8")
            conntrack_count = root / "nf_conntrack_count"
            conntrack_count.write_text("100\n", encoding="utf-8")
            conntrack_max = root / "nf_conntrack_max"
            conntrack_max.write_text("1000\n", encoding="utf-8")
            threads_max = root / "threads-max"
            threads_max.write_text("1000\n", encoding="utf-8")

            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
            env["IRLIGHT_HOST_COMPONENT_TIMEOUT_SECONDS"] = timeout_seconds
            env["IRLIGHT_TEST_DF_DELAY_SECONDS"] = df_delay_seconds

            return subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    str(state_dir),
                    str(meminfo),
                    str(loadavg),
                    "4",
                    str(psi_dir),
                    str(file_nr),
                    str(conntrack_count),
                    str(conntrack_max),
                    str(threads_max),
                ],
                env=env,
                text=True,
                capture_output=True,
                check=False,
                timeout=6,
            )

    def test_wedged_component_becomes_unknown_without_blocking_aggregate(self) -> None:
        result = self._run(timeout_seconds="1", df_delay_seconds="3")
        self.assertEqual(result.returncode, 3)
        self.assertIn("status=UNKNOWN", result.stdout)
        self.assertIn("disk_status=UNKNOWN", result.stdout)
        self.assertIn("memory_status=OK", result.stdout)
        self.assertIn("task_status=OK", result.stdout)

    def test_invalid_timeout_fails_closed_instead_of_running_unbounded(self) -> None:
        result = self._run(timeout_seconds="0", df_delay_seconds="3")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_HOST_PRESSURE status=UNKNOWN disk_status=UNKNOWN memory_status=UNKNOWN load_status=UNKNOWN psi_status=UNKNOWN file_handle_status=UNKNOWN conntrack_status=UNKNOWN task_status=UNKNOWN",
        )


if __name__ == "__main__":
    unittest.main()
