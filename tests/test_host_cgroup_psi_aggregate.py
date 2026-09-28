from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-host-pressure.sh"
CGROUP_WRAPPER = ROOT / "scripts" / "check-host-cgroup-psi-pressure.sh"
CGROUP_CHECKER = ROOT / "scripts" / "check-cgroup-psi-pressure.sh"
PSI_HELPER = ROOT / "scripts" / "lib" / "psi-pressure-common.sh"


class HostCgroupPsiAggregateTest(unittest.TestCase):
    def _run(
        self,
        *,
        mode: str | None = None,
        cpu_some: str = "1.00",
        memory_full: str = "0.00",
        component_code: int = 0,
        configure_target: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="irlight-host-cgroup-psi-aggregate-") as temporary:
            root = Path(temporary)
            scripts = root / "scripts"
            lib = scripts / "lib"
            cgroup = root / "target"
            lib.mkdir(parents=True)
            cgroup.mkdir()

            shutil.copyfile(SCRIPT, scripts / "check-host-pressure.sh")
            shutil.copyfile(CGROUP_WRAPPER, scripts / "check-host-cgroup-psi-pressure.sh")
            shutil.copyfile(CGROUP_CHECKER, scripts / "check-cgroup-psi-pressure.sh")
            shutil.copyfile(PSI_HELPER, lib / "psi-pressure-common.sh")

            component = "#!/usr/bin/env bash\n" + 'exit "${IRLIGHT_TEST_COMPONENT_CODE:-0}"\n'
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

            (cgroup / "cpu.pressure").write_text(
                f"some avg10={cpu_some} avg60=0.50 avg300=0.25 total=12345\n",
                encoding="ascii",
            )
            (cgroup / "memory.pressure").write_text(
                f"some avg10=1.00 avg60=0.50 avg300=0.25 total=12345\n"
                f"full avg10={memory_full} avg60=0.10 avg300=0.05 total=1234\n",
                encoding="ascii",
            )
            (cgroup / "io.pressure").write_text(
                "some avg10=1.00 avg60=0.50 avg300=0.25 total=12345\n"
                "full avg10=0.00 avg60=0.10 avg300=0.05 total=1234\n",
                encoding="ascii",
            )

            env = os.environ.copy()
            env["IRLIGHT_TEST_COMPONENT_CODE"] = str(component_code)
            for key in tuple(env):
                if key.startswith("IRLIGHT_HOST_") and key.endswith("_MODE"):
                    env.pop(key)
            if mode is not None:
                env["IRLIGHT_HOST_CGROUP_PSI_MODE"] = mode
            if configure_target:
                env["IRLIGHT_CGROUP_PSI_DIR"] = str(cgroup)
            else:
                env.pop("IRLIGHT_CGROUP_PSI_DIR", None)

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
        result = self._run(memory_full="20.00")
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("cgroup_psi_status", result.stdout)

    def test_warning_is_aggregated(self) -> None:
        result = self._run(mode="enabled", cpu_some="25.00")
        self.assertEqual(result.returncode, 1)
        self.assertIn("status=WARNING", result.stdout)
        self.assertIn("cgroup_psi_status=WARNING", result.stdout)

    def test_critical_is_aggregated(self) -> None:
        result = self._run(mode="enabled", memory_full="20.00")
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("cgroup_psi_status=CRITICAL", result.stdout)

    def test_missing_target_is_unknown(self) -> None:
        result = self._run(mode="enabled", configure_target=False)
        self.assertEqual(result.returncode, 3)
        self.assertIn("cgroup_psi_status=UNKNOWN", result.stdout)

    def test_other_critical_wins_over_cgroup_unknown(self) -> None:
        result = self._run(mode="enabled", configure_target=False, component_code=2)
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("cgroup_psi_status=UNKNOWN", result.stdout)

    def test_invalid_mode_fails_closed(self) -> None:
        result = self._run(mode="sometimes")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_HOST_PRESSURE status=UNKNOWN reason=invalid_cgroup_psi_mode",
        )


if __name__ == "__main__":
    unittest.main()
