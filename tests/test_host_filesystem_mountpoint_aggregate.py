from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-host-pressure.sh"
ADAPTER = ROOT / "scripts" / "check-host-filesystem-mountpoint.sh"


class HostFilesystemMountpointAggregateTest(unittest.TestCase):
    def _run(
        self,
        *,
        filesystem_mountpoint_mode: str | None = None,
        component_code: int = 0,
        filesystem_mountpoint_code: int = 0,
        expected_checker_path: str = "/state",
        expected_mountpoint_env: str | None = None,
        state_dir: str | None = None,
        disk_path: str = "disk",
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(
            prefix="irlight-host-filesystem-mountpoint-aggregate-"
        ) as temporary:
            root = Path(temporary)
            scripts = root / "scripts"
            scripts.mkdir()
            shutil.copyfile(SCRIPT, scripts / "check-host-pressure.sh")
            shutil.copyfile(ADAPTER, scripts / "check-host-filesystem-mountpoint.sh")

            component = (
                "#!/usr/bin/env bash\n"
                'exit "${IRLIGHT_TEST_COMPONENT_CODE:-0}"\n'
            )
            for name in (
                "check-disk-pressure.sh",
                "check-memory-pressure.sh",
                "check-load-pressure.sh",
                "check-psi-pressure.sh",
                "check-file-handle-pressure.sh",
                "check-conntrack-pressure.sh",
                "check-task-pressure.sh",
            ):
                (scripts / name).write_text(component, encoding="utf-8")

            (scripts / "check-filesystem-mountpoint.py").write_text(
                "import os\n"
                "import sys\n"
                "expected = os.environ.get('IRLIGHT_TEST_MOUNTPOINT_PATH')\n"
                "actual = sys.argv[1] if len(sys.argv) > 1 else None\n"
                "if actual != expected:\n"
                "    raise SystemExit(3)\n"
                "raise SystemExit(int(os.environ.get('IRLIGHT_TEST_FILESYSTEM_MOUNTPOINT_CODE', '0')))\n",
                encoding="utf-8",
            )

            env = os.environ.copy()
            env["IRLIGHT_TEST_COMPONENT_CODE"] = str(component_code)
            env["IRLIGHT_TEST_FILESYSTEM_MOUNTPOINT_CODE"] = str(
                filesystem_mountpoint_code
            )
            env["IRLIGHT_TEST_MOUNTPOINT_PATH"] = expected_checker_path
            if expected_mountpoint_env is not None:
                env["IRLIGHT_EXPECTED_MOUNTPOINT_PATH"] = expected_mountpoint_env
            else:
                env.pop("IRLIGHT_EXPECTED_MOUNTPOINT_PATH", None)
            if state_dir is not None:
                env["STATE_DIR"] = state_dir
            else:
                env.pop("STATE_DIR", None)
            if filesystem_mountpoint_mode is not None:
                env["IRLIGHT_HOST_FILESYSTEM_MOUNTPOINT_MODE"] = (
                    filesystem_mountpoint_mode
                )
            else:
                env.pop("IRLIGHT_HOST_FILESYSTEM_MOUNTPOINT_MODE", None)

            return subprocess.run(
                [
                    "bash",
                    str(scripts / "check-host-pressure.sh"),
                    disk_path,
                    "meminfo",
                    "loadavg",
                    "4",
                    "psi",
                    "file-nr",
                    "conntrack-count",
                    "conntrack-max",
                    "threads-max",
                ],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

    def test_default_output_contract_is_unchanged(self) -> None:
        result = self._run(filesystem_mountpoint_code=2)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_HOST_PRESSURE status=OK disk_status=OK memory_status=OK load_status=OK psi_status=OK file_handle_status=OK conntrack_status=OK task_status=OK",
        )
        self.assertNotIn("filesystem_mountpoint_status", result.stdout)

    def test_enabled_filesystem_mountpoint_ok_is_aggregated(self) -> None:
        result = self._run(
            filesystem_mountpoint_mode="enabled",
            filesystem_mountpoint_code=0,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("status=OK", result.stdout)
        self.assertIn("filesystem_mountpoint_status=OK", result.stdout)

    def test_enabled_filesystem_mountpoint_critical_is_aggregated(self) -> None:
        result = self._run(
            filesystem_mountpoint_mode="enabled",
            filesystem_mountpoint_code=2,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("filesystem_mountpoint_status=CRITICAL", result.stdout)

    def test_enabled_filesystem_mountpoint_unknown_is_fail_closed(self) -> None:
        result = self._run(
            filesystem_mountpoint_mode="enabled",
            filesystem_mountpoint_code=3,
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("status=UNKNOWN", result.stdout)
        self.assertIn("filesystem_mountpoint_status=UNKNOWN", result.stdout)

    def test_other_critical_wins_over_filesystem_mountpoint_unknown(self) -> None:
        result = self._run(
            filesystem_mountpoint_mode="enabled",
            component_code=2,
            filesystem_mountpoint_code=3,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("filesystem_mountpoint_status=UNKNOWN", result.stdout)

    def test_invalid_filesystem_mountpoint_mode_fails_closed_before_components(self) -> None:
        result = self._run(filesystem_mountpoint_mode="sometimes")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_HOST_PRESSURE status=UNKNOWN reason=invalid_filesystem_mountpoint_mode",
        )

    def test_explicit_expected_mountpoint_path_is_forwarded(self) -> None:
        result = self._run(
            filesystem_mountpoint_mode="enabled",
            expected_checker_path="/srv/irlight-state",
            expected_mountpoint_env="/srv/irlight-state",
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("filesystem_mountpoint_status=OK", result.stdout)

    def test_state_dir_is_forwarded_when_explicit_target_is_absent(self) -> None:
        result = self._run(
            filesystem_mountpoint_mode="enabled",
            expected_checker_path="/var/lib/irlight/state",
            state_dir="/var/lib/irlight/state",
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("filesystem_mountpoint_status=OK", result.stdout)

    def test_disk_path_does_not_replace_default_expected_mountpoint(self) -> None:
        result = self._run(
            filesystem_mountpoint_mode="enabled",
            expected_checker_path="/state",
            disk_path="/",
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("filesystem_mountpoint_status=OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
