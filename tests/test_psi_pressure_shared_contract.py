from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HOST_SCRIPT = ROOT / "scripts" / "check-psi-pressure.sh"
CGROUP_SCRIPT = ROOT / "scripts" / "check-cgroup-psi-pressure.sh"


class PsiPressureSharedContractTest(unittest.TestCase):
    def _run_pair(
        self,
        *,
        cpu_some: str = "1.00",
        memory_full: str = "0.00",
        cpu_avg60: str = "0.50",
    ) -> tuple[subprocess.CompletedProcess[str], subprocess.CompletedProcess[str]]:
        with tempfile.TemporaryDirectory(prefix="irlight-psi-contract-") as temporary:
            root = Path(temporary)
            host_dir = root / "host"
            cgroup_dir = root / "cgroup"
            host_dir.mkdir()
            cgroup_dir.mkdir()

            cpu = f"some avg10={cpu_some} avg60={cpu_avg60} avg300=0.25 total=12345\n"
            memory = (
                "some avg10=1.00 avg60=0.50 avg300=0.25 total=12345\n"
                f"full avg10={memory_full} avg60=0.10 avg300=0.05 total=1234\n"
            )
            io = (
                "some avg10=1.00 avg60=0.50 avg300=0.25 total=12345\n"
                "full avg10=0.00 avg60=0.10 avg300=0.05 total=1234\n"
            )

            for host_name, cgroup_name, content in (
                ("cpu", "cpu.pressure", cpu),
                ("memory", "memory.pressure", memory),
                ("io", "io.pressure", io),
            ):
                (host_dir / host_name).write_text(content, encoding="utf-8")
                (cgroup_dir / cgroup_name).write_text(content, encoding="utf-8")

            host = subprocess.run(
                ["bash", str(HOST_SCRIPT), str(host_dir)],
                text=True,
                capture_output=True,
                check=False,
            )
            cgroup = subprocess.run(
                ["bash", str(CGROUP_SCRIPT), str(cgroup_dir)],
                text=True,
                capture_output=True,
                check=False,
            )
            return host, cgroup

    def _assert_same_contract(
        self,
        host: subprocess.CompletedProcess[str],
        cgroup: subprocess.CompletedProcess[str],
    ) -> None:
        self.assertEqual(host.returncode, cgroup.returncode)
        self.assertEqual(
            host.stdout.replace("IRLIGHT_PSI_PRESSURE", "IRLIGHT_CGROUP_PSI_PRESSURE"),
            cgroup.stdout,
        )

    def test_warning_status_matches(self) -> None:
        self._assert_same_contract(*self._run_pair(cpu_some="25.00"))

    def test_critical_status_matches(self) -> None:
        self._assert_same_contract(*self._run_pair(memory_full="20.00"))

    def test_malformed_unused_average_fails_closed_identically(self) -> None:
        self._assert_same_contract(*self._run_pair(cpu_avg60="NaN"))


if __name__ == "__main__":
    unittest.main()
