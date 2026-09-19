from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-host-pressure.sh"
ADAPTER = ROOT / "scripts" / "check-host-filesystem-readonly.sh"


class HostFilesystemReadonlyAggregateTest(unittest.TestCase):
    def _run(
        self,
        *,
        filesystem_readonly_mode: str | None = None,
        component_code: int = 0,
        filesystem_readonly_code: int = 0,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(
            prefix="irlight-host-filesystem-readonly-aggregate-"
        ) as temporary:
            root = Path(temporary)
            scripts = root / "scripts"
            scripts.mkdir()
            shutil.copyfile(SCRIPT, scripts / "check-host-pressure.sh")
            shutil.copyfile(ADAPTER, scripts / "check-host-filesystem-readonly.sh")

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

            expected_path = root / "state"
            (scripts / "check-filesystem-readonly.py").write_text(
                "import os\n"
                "import sys\n"
                "expected = os.environ.get('IRLIGHT_TEST_FILESYSTEM_PATH')\n"
                "actual = sys.argv[1] if len(sys.argv) > 1 else None\n"
                "if actual != expected:\n"
                "    raise SystemExit(3)\n"
                "raise SystemExit(int(os.environ.get('IRLIGHT_TEST_FILESYSTEM_READONLY_CODE', '0')))\n",
                encoding="utf-8",
            )

            env = os.environ.copy()
            env["IRLIGHT_TEST_COMPONENT_CODE"] = str(component_code)
            env["IRLIGHT_TEST_FILESYSTEM_READONLY_CODE"] = str(
                filesystem_readonly_code
            )
            env["IRLIGHT_TEST_FILESYSTEM_PATH"] = str(expected_path)
            env["IRLIGHT_FILESYSTEM_PATH"] = str(expected_path)
            if filesystem_readonly_mode is not None:
                env["IRLIGHT_HOST_FILESYSTEM_READONLY_MODE"] = filesystem_readonly_mode
            else:
                env.pop("IRLIGHT_HOST_FILESYSTEM_READONLY_MODE", None)

            return subprocess.run(
                [
                    "bash",
                    str(scripts / "check-host-pressure.sh"),
                    "disk",
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
        result = self._run(filesystem_readonly_code=2)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_HOST_PRESSURE status=OK disk_status=OK memory_status=OK load_status=OK psi_status=OK file_handle_status=OK conntrack_status=OK task_status=OK",
        )
        self.assertNotIn("filesystem_readonly_status", result.stdout)

    def test_enabled_filesystem_readonly_ok_is_aggregated(self) -> None:
        result = self._run(
            filesystem_readonly_mode="enabled",
            filesystem_readonly_code=0,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("status=OK", result.stdout)
        self.assertIn("filesystem_readonly_status=OK", result.stdout)

    def test_enabled_filesystem_readonly_critical_is_aggregated(self) -> None:
        result = self._run(
            filesystem_readonly_mode="enabled",
            filesystem_readonly_code=2,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("filesystem_readonly_status=CRITICAL", result.stdout)

    def test_enabled_filesystem_readonly_unknown_is_fail_closed(self) -> None:
        result = self._run(
            filesystem_readonly_mode="enabled",
            filesystem_readonly_code=3,
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("status=UNKNOWN", result.stdout)
        self.assertIn("filesystem_readonly_status=UNKNOWN", result.stdout)

    def test_other_critical_wins_over_filesystem_readonly_unknown(self) -> None:
        result = self._run(
            filesystem_readonly_mode="enabled",
            component_code=2,
            filesystem_readonly_code=3,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("filesystem_readonly_status=UNKNOWN", result.stdout)

    def test_invalid_filesystem_readonly_mode_fails_closed_before_components(self) -> None:
        result = self._run(filesystem_readonly_mode="sometimes")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_HOST_PRESSURE status=UNKNOWN reason=invalid_filesystem_readonly_mode",
        )

    def test_enabled_mode_forwards_filesystem_path_to_checker(self) -> None:
        result = self._run(
            filesystem_readonly_mode="enabled",
            filesystem_readonly_code=0,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("filesystem_readonly_status=OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
