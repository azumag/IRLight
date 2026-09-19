from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-host-pressure.sh"


class HostSoftnetAggregateTest(unittest.TestCase):
    def _run(
        self,
        *,
        softnet_mode: str | None = None,
        component_code: int = 0,
        softnet_code: int = 0,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="irlight-host-softnet-aggregate-") as temporary:
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

            softnet = scripts / "check-softnet-pressure-delta.sh"
            softnet.write_text(
                "#!/usr/bin/env bash\n"
                "set -eu\n"
                '[[ "${1:-}" == "${IRLIGHT_TEST_SOFTNET_PATH:-}" ]] || exit 3\n'
                '[[ "${2:-}" == "${IRLIGHT_TEST_BASELINE_PATH:-}" ]] || exit 3\n'
                'exit "${IRLIGHT_TEST_SOFTNET_CODE:-0}"\n',
                encoding="utf-8",
            )

            current = root / "softnet_stat"
            current.write_text("00000001 00000000 00000000\n", encoding="utf-8")
            baseline = root / "baseline"
            baseline.write_text("00000001 00000000 00000000\n", encoding="utf-8")

            env = os.environ.copy()
            env["IRLIGHT_TEST_COMPONENT_CODE"] = str(component_code)
            env["IRLIGHT_TEST_SOFTNET_CODE"] = str(softnet_code)
            env["IRLIGHT_TEST_SOFTNET_PATH"] = str(current)
            env["IRLIGHT_TEST_BASELINE_PATH"] = str(baseline)
            env["IRLIGHT_SOFTNET_STAT_PATH"] = str(current)
            env["IRLIGHT_SOFTNET_STAT_BASELINE_PATH"] = str(baseline)
            if softnet_mode is not None:
                env["IRLIGHT_HOST_SOFTNET_MODE"] = softnet_mode
            else:
                env.pop("IRLIGHT_HOST_SOFTNET_MODE", None)

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
        self.assertNotIn("softnet_status", result.stdout)

    def test_enabled_softnet_warning_is_aggregated(self) -> None:
        result = self._run(softnet_mode="enabled", softnet_code=1)
        self.assertEqual(result.returncode, 1)
        self.assertIn("status=WARNING", result.stdout)
        self.assertIn("softnet_status=WARNING", result.stdout)

    def test_enabled_softnet_unknown_is_fail_closed(self) -> None:
        result = self._run(softnet_mode="enabled", softnet_code=3)
        self.assertEqual(result.returncode, 3)
        self.assertIn("status=UNKNOWN", result.stdout)
        self.assertIn("softnet_status=UNKNOWN", result.stdout)

    def test_other_critical_wins_over_softnet_unknown(self) -> None:
        result = self._run(softnet_mode="enabled", component_code=2, softnet_code=3)
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("softnet_status=UNKNOWN", result.stdout)

    def test_invalid_softnet_mode_fails_closed_before_components(self) -> None:
        result = self._run(softnet_mode="sometimes")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_HOST_PRESSURE status=UNKNOWN reason=invalid_softnet_mode",
        )

    def test_enabled_mode_forwards_softnet_and_baseline_paths(self) -> None:
        result = self._run(softnet_mode="enabled", softnet_code=0)
        self.assertEqual(result.returncode, 0)
        self.assertIn("softnet_status=OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
