from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-host-pressure.sh"
CGROUP_WRAPPER = ROOT / "scripts" / "check-host-cgroup-memory-high-pressure.sh"
CGROUP_CHECKER = ROOT / "scripts" / "check-cgroup-memory-high-pressure.sh"
SCALAR_HELPER = ROOT / "scripts" / "lib" / "scalar-pressure-common.sh"


class HostCgroupMemoryHighAggregateTest(unittest.TestCase):
    def _run(
        self,
        *,
        mode: str | None = None,
        current: str = "40",
        high: str = "100",
        component_code: int = 0,
        configure_current_path: bool = True,
        configure_high_path: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="irlight-host-cgroup-memory-high-aggregate-") as temporary:
            root = Path(temporary)
            scripts = root / "scripts"
            lib = scripts / "lib"
            lib.mkdir(parents=True)
            shutil.copyfile(SCRIPT, scripts / "check-host-pressure.sh")
            shutil.copyfile(CGROUP_WRAPPER, scripts / "check-host-cgroup-memory-high-pressure.sh")
            shutil.copyfile(CGROUP_CHECKER, scripts / "check-cgroup-memory-high-pressure.sh")
            shutil.copyfile(SCALAR_HELPER, lib / "scalar-pressure-common.sh")

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

            current_path = root / "memory.current"
            high_path = root / "memory.high"
            current_path.write_text(f"{current}\n", encoding="ascii")
            high_path.write_text(f"{high}\n", encoding="ascii")

            env = os.environ.copy()
            env["IRLIGHT_TEST_COMPONENT_CODE"] = str(component_code)
            if mode is not None:
                env["IRLIGHT_HOST_CGROUP_MEMORY_HIGH_MODE"] = mode
            else:
                env.pop("IRLIGHT_HOST_CGROUP_MEMORY_HIGH_MODE", None)

            if configure_current_path:
                env["IRLIGHT_CGROUP_MEMORY_HIGH_CURRENT_PATH"] = str(current_path)
            else:
                env.pop("IRLIGHT_CGROUP_MEMORY_HIGH_CURRENT_PATH", None)
            if configure_high_path:
                env["IRLIGHT_CGROUP_MEMORY_HIGH_PATH"] = str(high_path)
            else:
                env.pop("IRLIGHT_CGROUP_MEMORY_HIGH_PATH", None)

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
        result = self._run(current="99")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_HOST_PRESSURE status=OK disk_status=OK memory_status=OK load_status=OK psi_status=OK file_handle_status=OK conntrack_status=OK task_status=OK",
        )
        self.assertNotIn("cgroup_memory_high_status", result.stdout)

    def test_enabled_below_warning_is_ok(self) -> None:
        result = self._run(mode="enabled", current="79")
        self.assertEqual(result.returncode, 0)
        self.assertIn("status=OK", result.stdout)
        self.assertIn("cgroup_memory_high_status=OK", result.stdout)

    def test_enabled_at_warning_is_warning(self) -> None:
        result = self._run(mode="enabled", current="80")
        self.assertEqual(result.returncode, 1)
        self.assertIn("status=WARNING", result.stdout)
        self.assertIn("cgroup_memory_high_status=WARNING", result.stdout)

    def test_enabled_at_critical_is_critical(self) -> None:
        result = self._run(mode="enabled", current="90")
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("cgroup_memory_high_status=CRITICAL", result.stdout)

    def test_enabled_unlimited_high_is_ok(self) -> None:
        result = self._run(mode="enabled", current="90", high="max")
        self.assertEqual(result.returncode, 0)
        self.assertIn("status=OK", result.stdout)
        self.assertIn("cgroup_memory_high_status=OK", result.stdout)

    def test_enabled_zero_high_is_critical(self) -> None:
        result = self._run(mode="enabled", current="0", high="0")
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("cgroup_memory_high_status=CRITICAL", result.stdout)

    def test_enabled_over_high_is_critical(self) -> None:
        result = self._run(mode="enabled", current="101", high="100")
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("cgroup_memory_high_status=CRITICAL", result.stdout)

    def test_enabled_malformed_input_is_unknown(self) -> None:
        result = self._run(mode="enabled", current="not-a-number")
        self.assertEqual(result.returncode, 3)
        self.assertIn("status=UNKNOWN", result.stdout)
        self.assertIn("cgroup_memory_high_status=UNKNOWN", result.stdout)

    def test_enabled_without_current_target_is_unknown(self) -> None:
        result = self._run(mode="enabled", configure_current_path=False)
        self.assertEqual(result.returncode, 3)
        self.assertIn("status=UNKNOWN", result.stdout)
        self.assertIn("cgroup_memory_high_status=UNKNOWN", result.stdout)

    def test_enabled_without_high_target_is_unknown(self) -> None:
        result = self._run(mode="enabled", configure_high_path=False)
        self.assertEqual(result.returncode, 3)
        self.assertIn("status=UNKNOWN", result.stdout)
        self.assertIn("cgroup_memory_high_status=UNKNOWN", result.stdout)

    def test_enabled_without_explicit_target_is_unknown(self) -> None:
        result = self._run(
            mode="enabled",
            configure_current_path=False,
            configure_high_path=False,
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("status=UNKNOWN", result.stdout)
        self.assertIn("cgroup_memory_high_status=UNKNOWN", result.stdout)

    def test_other_critical_wins_over_cgroup_unknown(self) -> None:
        result = self._run(
            mode="enabled",
            configure_current_path=False,
            component_code=2,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("cgroup_memory_high_status=UNKNOWN", result.stdout)

    def test_invalid_mode_fails_closed_before_components(self) -> None:
        result = self._run(mode="sometimes")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_HOST_PRESSURE status=UNKNOWN reason=invalid_cgroup_memory_high_mode",
        )


if __name__ == "__main__":
    unittest.main()
