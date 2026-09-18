from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-host-pressure.sh"


class HostBootGenerationAggregateTest(unittest.TestCase):
    def _run(
        self,
        *,
        boot_generation_mode: str | None = None,
        component_code: int = 0,
        boot_generation_code: int = 0,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="irlight-host-boot-generation-aggregate-") as temporary:
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

            boot_generation = scripts / "check-host-boot-generation.sh"
            boot_generation.write_text(
                "#!/usr/bin/env bash\n"
                "set -eu\n"
                '[[ "${1:-}" == "${IRLIGHT_TEST_BOOT_ID_PATH:-}" ]] || exit 3\n'
                '[[ "${2:-}" == "${IRLIGHT_TEST_BASELINE_PATH:-}" ]] || exit 3\n'
                'exit "${IRLIGHT_TEST_BOOT_GENERATION_CODE:-0}"\n',
                encoding="utf-8",
            )

            boot_id = root / "boot-id"
            boot_id.write_text("11111111-2222-3333-4444-555555555555\n", encoding="utf-8")
            baseline = root / "baseline"
            baseline.write_text("11111111-2222-3333-4444-555555555555\n", encoding="utf-8")

            env = os.environ.copy()
            env["IRLIGHT_TEST_COMPONENT_CODE"] = str(component_code)
            env["IRLIGHT_TEST_BOOT_GENERATION_CODE"] = str(boot_generation_code)
            env["IRLIGHT_TEST_BOOT_ID_PATH"] = str(boot_id)
            env["IRLIGHT_TEST_BASELINE_PATH"] = str(baseline)
            env["IRLIGHT_BOOT_ID_PATH"] = str(boot_id)
            env["IRLIGHT_BOOT_ID_BASELINE_PATH"] = str(baseline)
            env["IRLIGHT_HOST_SWAP_IO_MODE"] = "disabled"
            env["IRLIGHT_HOST_CPU_STEAL_MODE"] = "disabled"
            if boot_generation_mode is not None:
                env["IRLIGHT_HOST_BOOT_GENERATION_MODE"] = boot_generation_mode
            else:
                env.pop("IRLIGHT_HOST_BOOT_GENERATION_MODE", None)

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
        self.assertNotIn("boot_generation_status", result.stdout)

    def test_enabled_boot_generation_warning_is_aggregated(self) -> None:
        result = self._run(boot_generation_mode="enabled", boot_generation_code=1)
        self.assertEqual(result.returncode, 1)
        self.assertIn("status=WARNING", result.stdout)
        self.assertIn("boot_generation_status=WARNING", result.stdout)

    def test_enabled_boot_generation_unknown_is_fail_closed(self) -> None:
        result = self._run(boot_generation_mode="enabled", boot_generation_code=3)
        self.assertEqual(result.returncode, 3)
        self.assertIn("status=UNKNOWN", result.stdout)
        self.assertIn("boot_generation_status=UNKNOWN", result.stdout)

    def test_known_critical_still_wins_over_boot_generation_unknown(self) -> None:
        result = self._run(
            boot_generation_mode="enabled",
            component_code=2,
            boot_generation_code=3,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("boot_generation_status=UNKNOWN", result.stdout)

    def test_invalid_boot_generation_mode_fails_closed_before_components(self) -> None:
        result = self._run(boot_generation_mode="sometimes")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_HOST_PRESSURE status=UNKNOWN reason=invalid_boot_generation_mode",
        )

    def test_enabled_mode_forwards_boot_id_and_baseline_paths(self) -> None:
        result = self._run(boot_generation_mode="enabled", boot_generation_code=0)
        self.assertEqual(result.returncode, 0)
        self.assertIn("boot_generation_status=OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
