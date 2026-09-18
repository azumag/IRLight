from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-host-pressure.sh"


class HostOomKillAggregateTest(unittest.TestCase):
    def _run(
        self,
        *,
        oom_mode: str | None = None,
        component_code: int = 0,
        oom_code: int = 0,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="irlight-host-oom-kill-aggregate-") as temporary:
            root = Path(temporary)
            scripts = root / "scripts"
            scripts.mkdir()
            shutil.copyfile(SCRIPT, scripts / "check-host-pressure.sh")

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

            oom = scripts / "check-oom-kill-delta.sh"
            oom.write_text(
                "#!/usr/bin/env bash\n"
                "set -eu\n"
                '[[ "${1:-}" == "${IRLIGHT_TEST_VMSTAT_PATH:-}" ]] || exit 3\n'
                '[[ "${2:-}" == "${IRLIGHT_TEST_BASELINE_PATH:-}" ]] || exit 3\n'
                'exit "${IRLIGHT_TEST_OOM_CODE:-0}"\n',
                encoding="utf-8",
            )

            vmstat = root / "vmstat"
            vmstat.write_text("oom_kill 3\n", encoding="utf-8")
            baseline = root / "baseline"
            baseline.write_text("oom_kill 3\n", encoding="utf-8")

            env = os.environ.copy()
            env["IRLIGHT_TEST_COMPONENT_CODE"] = str(component_code)
            env["IRLIGHT_TEST_OOM_CODE"] = str(oom_code)
            env["IRLIGHT_TEST_VMSTAT_PATH"] = str(vmstat)
            env["IRLIGHT_TEST_BASELINE_PATH"] = str(baseline)
            env["IRLIGHT_VMSTAT_PATH"] = str(vmstat)
            env["IRLIGHT_VMSTAT_BASELINE_PATH"] = str(baseline)
            if oom_mode is not None:
                env["IRLIGHT_HOST_OOM_KILL_MODE"] = oom_mode
            else:
                env.pop("IRLIGHT_HOST_OOM_KILL_MODE", None)

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
        self.assertNotIn("oom_kill_status", result.stdout)

    def test_enabled_oom_kill_critical_is_aggregated(self) -> None:
        result = self._run(oom_mode="enabled", oom_code=2)
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("oom_kill_status=CRITICAL", result.stdout)

    def test_enabled_oom_kill_unknown_is_fail_closed(self) -> None:
        result = self._run(oom_mode="enabled", oom_code=3)
        self.assertEqual(result.returncode, 3)
        self.assertIn("status=UNKNOWN", result.stdout)
        self.assertIn("oom_kill_status=UNKNOWN", result.stdout)

    def test_oom_kill_critical_wins_over_other_warning(self) -> None:
        result = self._run(oom_mode="enabled", component_code=1, oom_code=2)
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("oom_kill_status=CRITICAL", result.stdout)

    def test_invalid_oom_mode_fails_closed_before_components(self) -> None:
        result = self._run(oom_mode="sometimes")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_HOST_PRESSURE status=UNKNOWN reason=invalid_oom_kill_mode",
        )

    def test_enabled_mode_forwards_vmstat_and_baseline_paths(self) -> None:
        result = self._run(oom_mode="enabled", oom_code=0)
        self.assertEqual(result.returncode, 0)
        self.assertIn("oom_kill_status=OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
