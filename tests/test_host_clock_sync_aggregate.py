from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-host-pressure.sh"
ADAPTER = ROOT / "scripts" / "check-host-clock-sync.sh"


class HostClockSyncAggregateTest(unittest.TestCase):
    def _run(
        self,
        *,
        clock_sync_mode: str | None = None,
        component_code: int = 0,
        clock_sync_code: int = 0,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="irlight-host-clock-sync-aggregate-") as temporary:
            root = Path(temporary)
            scripts = root / "scripts"
            scripts.mkdir()
            shutil.copyfile(SCRIPT, scripts / "check-host-pressure.sh")
            shutil.copyfile(ADAPTER, scripts / "check-host-clock-sync.sh")

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

            (scripts / "check-host-clock-sync.py").write_text(
                "import os\n"
                "import sys\n"
                "expected = os.environ.get('IRLIGHT_TEST_TIMEDATECTL_BIN')\n"
                "actual = os.environ.get('IRLIGHT_TIMEDATECTL_BIN')\n"
                "if actual != expected:\n"
                "    raise SystemExit(3)\n"
                "raise SystemExit(int(os.environ.get('IRLIGHT_TEST_CLOCK_SYNC_CODE', '0')))\n",
                encoding="utf-8",
            )

            fake_timedatectl = root / "fake-timedatectl"
            fake_timedatectl.write_text("unused\n", encoding="utf-8")

            env = os.environ.copy()
            env["IRLIGHT_TEST_COMPONENT_CODE"] = str(component_code)
            env["IRLIGHT_TEST_CLOCK_SYNC_CODE"] = str(clock_sync_code)
            env["IRLIGHT_TEST_TIMEDATECTL_BIN"] = str(fake_timedatectl)
            env["IRLIGHT_TIMEDATECTL_BIN"] = str(fake_timedatectl)
            if clock_sync_mode is not None:
                env["IRLIGHT_HOST_CLOCK_SYNC_MODE"] = clock_sync_mode
            else:
                env.pop("IRLIGHT_HOST_CLOCK_SYNC_MODE", None)

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
        result = self._run()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_HOST_PRESSURE status=OK disk_status=OK memory_status=OK load_status=OK psi_status=OK file_handle_status=OK conntrack_status=OK task_status=OK",
        )
        self.assertNotIn("clock_sync_status", result.stdout)

    def test_enabled_clock_sync_warning_is_aggregated(self) -> None:
        result = self._run(clock_sync_mode="enabled", clock_sync_code=1)
        self.assertEqual(result.returncode, 1)
        self.assertIn("status=WARNING", result.stdout)
        self.assertIn("clock_sync_status=WARNING", result.stdout)

    def test_enabled_clock_sync_unknown_is_fail_closed(self) -> None:
        result = self._run(clock_sync_mode="enabled", clock_sync_code=3)
        self.assertEqual(result.returncode, 3)
        self.assertIn("status=UNKNOWN", result.stdout)
        self.assertIn("clock_sync_status=UNKNOWN", result.stdout)

    def test_other_critical_wins_over_clock_sync_unknown(self) -> None:
        result = self._run(
            clock_sync_mode="enabled",
            component_code=2,
            clock_sync_code=3,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("clock_sync_status=UNKNOWN", result.stdout)

    def test_invalid_clock_sync_mode_fails_closed_before_components(self) -> None:
        result = self._run(clock_sync_mode="sometimes")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_HOST_PRESSURE status=UNKNOWN reason=invalid_clock_sync_mode",
        )

    def test_enabled_mode_preserves_clock_checker_environment(self) -> None:
        result = self._run(clock_sync_mode="enabled", clock_sync_code=0)
        self.assertEqual(result.returncode, 0)
        self.assertIn("clock_sync_status=OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
