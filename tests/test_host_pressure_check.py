from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-host-pressure.sh"


class HostPressureCheckTest(unittest.TestCase):
    def _run(
        self,
        *,
        disk_usage: str = "10",
        inode_usage: str = "10",
        available_kb: str = "500",
        meminfo_valid: bool = True,
        load5: str = "0.20",
        loadavg_valid: bool = True,
        cpu_count: str = "4",
        psi_cpu_some: str = "1.00",
        psi_memory_full: str = "0.00",
        psi_valid: bool = True,
        file_nr: str = "100 0 1000\n",
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="irlight-host-pressure-") as temporary:
            root = Path(temporary)
            state_dir = root / "state"
            state_dir.mkdir()

            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_df = fake_bin / "df"
            fake_df.write_text(
                """#!/usr/bin/env bash
set -eu
case "${1:-}" in
  -P)
    printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\\n'
    printf 'fake 1000 100 900 %s%% /fake\\n' "${IRLIGHT_TEST_DISK_USAGE}"
    ;;
  -Pi)
    printf 'Filesystem Inodes IUsed IFree IUse%% Mounted on\\n'
    printf 'fake 1000 100 900 %s%% /fake\\n' "${IRLIGHT_TEST_INODE_USAGE}"
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
            if meminfo_valid:
                meminfo.write_text(
                    f"MemTotal: 1000 kB\nMemAvailable: {available_kb} kB\n",
                    encoding="utf-8",
                )
            else:
                meminfo.write_text("MemTotal: 1000 kB\n", encoding="utf-8")

            loadavg = root / "loadavg"
            if loadavg_valid:
                loadavg.write_text(
                    f"0.10 {load5} 0.30 1/100 123\n",
                    encoding="utf-8",
                )
            else:
                loadavg.write_text("0.10\n", encoding="utf-8")

            psi_dir = root / "pressure"
            psi_dir.mkdir()
            if psi_valid:
                (psi_dir / "cpu").write_text(
                    f"some avg10={psi_cpu_some} avg60=0.50 avg300=0.25 total=100\n",
                    encoding="utf-8",
                )
                (psi_dir / "memory").write_text(
                    "some avg10=1.00 avg60=0.50 avg300=0.25 total=100\n"
                    f"full avg10={psi_memory_full} avg60=0.10 avg300=0.05 total=10\n",
                    encoding="utf-8",
                )
                (psi_dir / "io").write_text(
                    "some avg10=1.00 avg60=0.50 avg300=0.25 total=100\n"
                    "full avg10=0.00 avg60=0.10 avg300=0.05 total=10\n",
                    encoding="utf-8",
                )
            else:
                (psi_dir / "cpu").write_text("invalid\n", encoding="utf-8")
                (psi_dir / "memory").write_text("invalid\n", encoding="utf-8")
                (psi_dir / "io").write_text("invalid\n", encoding="utf-8")

            file_nr_path = root / "file-nr"
            file_nr_path.write_text(file_nr, encoding="utf-8")

            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
            env["IRLIGHT_TEST_DISK_USAGE"] = disk_usage
            env["IRLIGHT_TEST_INODE_USAGE"] = inode_usage

            return subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    str(state_dir),
                    str(meminfo),
                    str(loadavg),
                    cpu_count,
                    str(psi_dir),
                    str(file_nr_path),
                ],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

    def test_ok_when_all_components_are_ok(self) -> None:
        result = self._run()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_HOST_PRESSURE status=OK disk_status=OK memory_status=OK load_status=OK psi_status=OK file_handle_status=OK",
        )

    def test_warning_propagates_from_memory(self) -> None:
        result = self._run(available_kb="150")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_HOST_PRESSURE status=WARNING disk_status=OK memory_status=WARNING load_status=OK psi_status=OK file_handle_status=OK",
        )

    def test_warning_propagates_from_inode_pressure(self) -> None:
        result = self._run(inode_usage="85")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_HOST_PRESSURE status=WARNING disk_status=WARNING memory_status=OK load_status=OK psi_status=OK file_handle_status=OK",
        )

    def test_warning_propagates_from_load_pressure(self) -> None:
        result = self._run(load5="4.00")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_HOST_PRESSURE status=WARNING disk_status=OK memory_status=OK load_status=WARNING psi_status=OK file_handle_status=OK",
        )

    def test_warning_propagates_from_psi_pressure(self) -> None:
        result = self._run(psi_cpu_some="25.00")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_HOST_PRESSURE status=WARNING disk_status=OK memory_status=OK load_status=OK psi_status=WARNING file_handle_status=OK",
        )

    def test_warning_propagates_from_file_handle_pressure(self) -> None:
        result = self._run(file_nr="850 0 1000\n")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_HOST_PRESSURE status=WARNING disk_status=OK memory_status=OK load_status=OK psi_status=OK file_handle_status=WARNING",
        )

    def test_unknown_is_fail_closed_over_warning(self) -> None:
        result = self._run(disk_usage="85", meminfo_valid=False)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_HOST_PRESSURE status=UNKNOWN disk_status=WARNING memory_status=UNKNOWN load_status=OK psi_status=OK file_handle_status=OK",
        )

    def test_load_unknown_is_fail_closed_over_warning(self) -> None:
        result = self._run(disk_usage="85", loadavg_valid=False)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_HOST_PRESSURE status=UNKNOWN disk_status=WARNING memory_status=OK load_status=UNKNOWN psi_status=OK file_handle_status=OK",
        )

    def test_psi_unknown_is_fail_closed_over_warning(self) -> None:
        result = self._run(disk_usage="85", psi_valid=False)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_HOST_PRESSURE status=UNKNOWN disk_status=WARNING memory_status=OK load_status=OK psi_status=UNKNOWN file_handle_status=OK",
        )

    def test_file_handle_unknown_is_fail_closed_over_warning(self) -> None:
        result = self._run(disk_usage="85", file_nr="invalid\n")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_HOST_PRESSURE status=UNKNOWN disk_status=WARNING memory_status=OK load_status=OK psi_status=OK file_handle_status=UNKNOWN",
        )

    def test_known_critical_is_not_hidden_by_unknown(self) -> None:
        result = self._run(disk_usage="95", meminfo_valid=False)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_HOST_PRESSURE status=CRITICAL disk_status=CRITICAL memory_status=UNKNOWN load_status=OK psi_status=OK file_handle_status=OK",
        )

    def test_load_critical_is_not_hidden_by_other_unknown(self) -> None:
        result = self._run(load5="8.00", meminfo_valid=False)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_HOST_PRESSURE status=CRITICAL disk_status=OK memory_status=UNKNOWN load_status=CRITICAL psi_status=OK file_handle_status=OK",
        )

    def test_psi_critical_is_not_hidden_by_other_unknown(self) -> None:
        result = self._run(psi_memory_full="20.00", meminfo_valid=False)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_HOST_PRESSURE status=CRITICAL disk_status=OK memory_status=UNKNOWN load_status=OK psi_status=CRITICAL file_handle_status=OK",
        )

    def test_file_handle_critical_is_not_hidden_by_other_unknown(self) -> None:
        result = self._run(file_nr="950 0 1000\n", meminfo_valid=False)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_HOST_PRESSURE status=CRITICAL disk_status=OK memory_status=UNKNOWN load_status=OK psi_status=OK file_handle_status=CRITICAL",
        )

    def test_invalid_component_output_becomes_unknown(self) -> None:
        result = self._run(disk_usage="invalid")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_HOST_PRESSURE status=UNKNOWN disk_status=UNKNOWN memory_status=OK load_status=OK psi_status=OK file_handle_status=OK",
        )


if __name__ == "__main__":
    unittest.main()
