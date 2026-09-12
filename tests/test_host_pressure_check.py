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
        conntrack_count: str = "100\n",
        conntrack_max: str = "1000\n",
        total_tasks: str = "100",
        threads_max: str = "1000\n",
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
                    f"0.10 {load5} 0.30 1/{total_tasks} 123\n",
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
            conntrack_count_path = root / "nf_conntrack_count"
            conntrack_count_path.write_text(conntrack_count, encoding="utf-8")
            conntrack_max_path = root / "nf_conntrack_max"
            conntrack_max_path.write_text(conntrack_max, encoding="utf-8")
            threads_max_path = root / "threads-max"
            threads_max_path.write_text(threads_max, encoding="utf-8")

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
                    str(conntrack_count_path),
                    str(conntrack_max_path),
                    str(threads_max_path),
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
            "IRLIGHT_HOST_PRESSURE status=OK disk_status=OK memory_status=OK load_status=OK psi_status=OK file_handle_status=OK conntrack_status=OK task_status=OK",
        )

    def test_warning_propagates_from_memory(self) -> None:
        result = self._run(available_kb="150")
        self.assertEqual(result.returncode, 1)
        self.assertIn("status=WARNING", result.stdout)
        self.assertIn("memory_status=WARNING", result.stdout)
        self.assertIn("task_status=OK", result.stdout)

    def test_warning_propagates_from_inode_pressure(self) -> None:
        result = self._run(inode_usage="85")
        self.assertEqual(result.returncode, 1)
        self.assertIn("disk_status=WARNING", result.stdout)

    def test_warning_propagates_from_load_pressure(self) -> None:
        result = self._run(load5="4.00")
        self.assertEqual(result.returncode, 1)
        self.assertIn("load_status=WARNING", result.stdout)

    def test_warning_propagates_from_psi_pressure(self) -> None:
        result = self._run(psi_cpu_some="25.00")
        self.assertEqual(result.returncode, 1)
        self.assertIn("psi_status=WARNING", result.stdout)

    def test_warning_propagates_from_file_handle_pressure(self) -> None:
        result = self._run(file_nr="850 0 1000\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("file_handle_status=WARNING", result.stdout)

    def test_warning_propagates_from_conntrack_pressure(self) -> None:
        result = self._run(conntrack_count="850\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("conntrack_status=WARNING", result.stdout)

    def test_warning_propagates_from_task_pressure(self) -> None:
        result = self._run(total_tasks="800")
        self.assertEqual(result.returncode, 1)
        self.assertIn("task_status=WARNING", result.stdout)

    def test_unknown_is_fail_closed_over_warning(self) -> None:
        result = self._run(disk_usage="85", meminfo_valid=False)
        self.assertEqual(result.returncode, 3)
        self.assertIn("status=UNKNOWN", result.stdout)
        self.assertIn("disk_status=WARNING", result.stdout)
        self.assertIn("memory_status=UNKNOWN", result.stdout)

    def test_load_unknown_is_fail_closed_over_warning(self) -> None:
        result = self._run(disk_usage="85", loadavg_valid=False)
        self.assertEqual(result.returncode, 3)
        self.assertIn("load_status=UNKNOWN", result.stdout)
        self.assertIn("task_status=UNKNOWN", result.stdout)

    def test_psi_unknown_is_fail_closed_over_warning(self) -> None:
        result = self._run(disk_usage="85", psi_valid=False)
        self.assertEqual(result.returncode, 3)
        self.assertIn("psi_status=UNKNOWN", result.stdout)

    def test_file_handle_unknown_is_fail_closed_over_warning(self) -> None:
        result = self._run(disk_usage="85", file_nr="invalid\n")
        self.assertEqual(result.returncode, 3)
        self.assertIn("file_handle_status=UNKNOWN", result.stdout)

    def test_conntrack_unknown_is_fail_closed_over_warning(self) -> None:
        result = self._run(disk_usage="85", conntrack_count="invalid\n")
        self.assertEqual(result.returncode, 3)
        self.assertIn("conntrack_status=UNKNOWN", result.stdout)

    def test_task_unknown_is_fail_closed_over_warning(self) -> None:
        result = self._run(disk_usage="85", threads_max="invalid\n")
        self.assertEqual(result.returncode, 3)
        self.assertIn("status=UNKNOWN", result.stdout)
        self.assertIn("task_status=UNKNOWN", result.stdout)

    def test_known_critical_is_not_hidden_by_unknown(self) -> None:
        result = self._run(disk_usage="95", meminfo_valid=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("disk_status=CRITICAL", result.stdout)
        self.assertIn("memory_status=UNKNOWN", result.stdout)

    def test_load_critical_is_not_hidden_by_other_unknown(self) -> None:
        result = self._run(load5="8.00", meminfo_valid=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("load_status=CRITICAL", result.stdout)

    def test_psi_critical_is_not_hidden_by_other_unknown(self) -> None:
        result = self._run(psi_memory_full="20.00", meminfo_valid=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("psi_status=CRITICAL", result.stdout)

    def test_file_handle_critical_is_not_hidden_by_other_unknown(self) -> None:
        result = self._run(file_nr="950 0 1000\n", meminfo_valid=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("file_handle_status=CRITICAL", result.stdout)

    def test_conntrack_critical_is_not_hidden_by_other_unknown(self) -> None:
        result = self._run(conntrack_count="950\n", meminfo_valid=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("conntrack_status=CRITICAL", result.stdout)

    def test_task_critical_is_not_hidden_by_other_unknown(self) -> None:
        result = self._run(total_tasks="950", meminfo_valid=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("memory_status=UNKNOWN", result.stdout)
        self.assertIn("task_status=CRITICAL", result.stdout)

    def test_mixed_severities_keep_details_while_critical_wins(self) -> None:
        result = self._run(
            disk_usage="85",
            meminfo_valid=False,
            total_tasks="950",
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("disk_status=WARNING", result.stdout)
        self.assertIn("memory_status=UNKNOWN", result.stdout)
        self.assertIn("task_status=CRITICAL", result.stdout)

    def test_invalid_component_output_becomes_unknown(self) -> None:
        result = self._run(disk_usage="invalid")
        self.assertEqual(result.returncode, 3)
        self.assertIn("status=UNKNOWN", result.stdout)
        self.assertIn("disk_status=UNKNOWN", result.stdout)


if __name__ == "__main__":
    unittest.main()
