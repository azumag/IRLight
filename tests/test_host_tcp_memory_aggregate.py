from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-host-pressure.sh"
TCP_WRAPPER = ROOT / "scripts" / "check-host-tcp-memory-pressure.sh"
TCP_CHECKER = ROOT / "scripts" / "check-host-tcp-memory-pressure.py"


class HostTcpMemoryAggregateTest(unittest.TestCase):
    def _run(
        self,
        *,
        tcp_memory_mode: str | None = None,
        mem_pages: int = 10,
        pressure_pages: int = 20,
        max_pages: int = 30,
        malformed_sockstat: bool = False,
        component_code: int = 0,
        configure_inputs: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="irlight-host-tcp-memory-aggregate-") as temporary:
            root = Path(temporary)
            scripts = root / "scripts"
            scripts.mkdir()
            shutil.copyfile(SCRIPT, scripts / "check-host-pressure.sh")
            shutil.copyfile(TCP_WRAPPER, scripts / "check-host-tcp-memory-pressure.sh")
            shutil.copyfile(TCP_CHECKER, scripts / "check-host-tcp-memory-pressure.py")

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

            sockstat = root / "sockstat"
            tcp_mem = root / "tcp_mem"
            if configure_inputs:
                if malformed_sockstat:
                    sockstat.write_text("TCP: inuse 1 mem nope\n", encoding="ascii")
                else:
                    sockstat.write_text(
                        f"TCP: inuse 1 orphan 0 tw 0 alloc 1 mem {mem_pages}\n",
                        encoding="ascii",
                    )
                tcp_mem.write_text(
                    f"1 {pressure_pages} {max_pages}\n",
                    encoding="ascii",
                )

            env = os.environ.copy()
            env["IRLIGHT_TEST_COMPONENT_CODE"] = str(component_code)
            if tcp_memory_mode is not None:
                env["IRLIGHT_HOST_TCP_MEMORY_MODE"] = tcp_memory_mode
            else:
                env.pop("IRLIGHT_HOST_TCP_MEMORY_MODE", None)
            env["IRLIGHT_TCP_SOCKSTAT_PATH"] = str(sockstat)
            env["IRLIGHT_TCP_MEM_PATH"] = str(tcp_mem)

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
        result = self._run(mem_pages=30)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_HOST_PRESSURE status=OK disk_status=OK memory_status=OK load_status=OK psi_status=OK file_handle_status=OK conntrack_status=OK task_status=OK",
        )
        self.assertNotIn("tcp_memory_status", result.stdout)

    def test_enabled_below_pressure_is_ok(self) -> None:
        result = self._run(tcp_memory_mode="enabled", mem_pages=10)
        self.assertEqual(result.returncode, 0)
        self.assertIn("status=OK", result.stdout)
        self.assertIn("tcp_memory_status=OK", result.stdout)

    def test_enabled_at_pressure_is_warning(self) -> None:
        result = self._run(tcp_memory_mode="enabled", mem_pages=20)
        self.assertEqual(result.returncode, 1)
        self.assertIn("status=WARNING", result.stdout)
        self.assertIn("tcp_memory_status=WARNING", result.stdout)

    def test_enabled_at_max_is_critical(self) -> None:
        result = self._run(tcp_memory_mode="enabled", mem_pages=30)
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("tcp_memory_status=CRITICAL", result.stdout)

    def test_enabled_malformed_input_is_unknown(self) -> None:
        result = self._run(
            tcp_memory_mode="enabled",
            malformed_sockstat=True,
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("status=UNKNOWN", result.stdout)
        self.assertIn("tcp_memory_status=UNKNOWN", result.stdout)

    def test_other_critical_wins_over_tcp_memory_unknown(self) -> None:
        result = self._run(
            tcp_memory_mode="enabled",
            configure_inputs=False,
            component_code=2,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("tcp_memory_status=UNKNOWN", result.stdout)

    def test_invalid_tcp_memory_mode_fails_closed_before_components(self) -> None:
        result = self._run(tcp_memory_mode="sometimes")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_HOST_PRESSURE status=UNKNOWN reason=invalid_tcp_memory_mode",
        )


if __name__ == "__main__":
    unittest.main()
