from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "scripts" / "check-host-cgroup-psi-pressure.sh"
CHECKER = ROOT / "scripts" / "check-cgroup-psi-pressure.sh"
HELPER = ROOT / "scripts" / "lib" / "psi-pressure-common.sh"


class HostCgroupPsiAdapterTest(unittest.TestCase):
    def _run(
        self,
        *,
        cpu_some: str = "1.00",
        memory_some: str = "1.00",
        memory_full: str = "0.00",
        io_some: str = "1.00",
        io_full: str = "0.00",
        configure_target: bool = True,
        include_checker: bool = True,
        include_helper: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="irlight-host-cgroup-psi-") as temporary:
            root = Path(temporary)
            scripts = root / "scripts"
            lib = scripts / "lib"
            cgroup = root / "target"
            lib.mkdir(parents=True)
            cgroup.mkdir()
            shutil.copyfile(WRAPPER, scripts / "check-host-cgroup-psi-pressure.sh")
            if include_checker:
                shutil.copyfile(CHECKER, scripts / "check-cgroup-psi-pressure.sh")
            if include_helper:
                shutil.copyfile(HELPER, lib / "psi-pressure-common.sh")

            (cgroup / "cpu.pressure").write_text(
                f"some avg10={cpu_some} avg60=0.50 avg300=0.25 total=12345\n",
                encoding="ascii",
            )
            (cgroup / "memory.pressure").write_text(
                f"some avg10={memory_some} avg60=0.50 avg300=0.25 total=12345\n"
                f"full avg10={memory_full} avg60=0.10 avg300=0.05 total=1234\n",
                encoding="ascii",
            )
            (cgroup / "io.pressure").write_text(
                f"some avg10={io_some} avg60=0.50 avg300=0.25 total=12345\n"
                f"full avg10={io_full} avg60=0.10 avg300=0.05 total=1234\n",
                encoding="ascii",
            )

            command = ["bash", str(scripts / "check-host-cgroup-psi-pressure.sh")]
            if configure_target:
                command.append(str(cgroup))
            env = {
                key: value
                for key, value in os.environ.items()
                if key != "IRLIGHT_CGROUP_PSI_DIR"
            }
            return subprocess.run(
                command,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

    def test_ok_is_forwarded(self) -> None:
        result = self._run()
        self.assertEqual(result.returncode, 0)
        self.assertIn("IRLIGHT_CGROUP_PSI_PRESSURE status=OK", result.stdout)

    def test_warning_is_forwarded(self) -> None:
        result = self._run(cpu_some="25.00")
        self.assertEqual(result.returncode, 1)
        self.assertIn("status=WARNING", result.stdout)

    def test_critical_is_forwarded(self) -> None:
        result = self._run(memory_full="20.00")
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)

    def test_missing_explicit_target_is_unknown(self) -> None:
        result = self._run(configure_target=False)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_CGROUP_PSI_PRESSURE status=UNKNOWN reason=target_not_configured",
        )

    def test_missing_checker_is_unknown(self) -> None:
        result = self._run(include_checker=False)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_CGROUP_PSI_PRESSURE status=UNKNOWN reason=checker_unavailable",
        )

    def test_missing_helper_is_unknown(self) -> None:
        result = self._run(include_helper=False)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_CGROUP_PSI_PRESSURE status=UNKNOWN reason=checker_unavailable",
        )


if __name__ == "__main__":
    unittest.main()
