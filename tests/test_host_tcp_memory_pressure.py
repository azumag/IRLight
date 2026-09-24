from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check-host-tcp-memory-pressure.py"


class HostTcpMemoryPressureTest(unittest.TestCase):
    def _run(self, sockstat: str, tcp_mem: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sockstat_path = root / "sockstat"
            tcp_mem_path = root / "tcp_mem"
            sockstat_path.write_text(sockstat, encoding="ascii")
            tcp_mem_path.write_text(tcp_mem, encoding="ascii")
            return subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--sockstat",
                    str(sockstat_path),
                    "--tcp-mem",
                    str(tcp_mem_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

    def test_below_pressure_is_ok(self) -> None:
        result = self._run("TCP: inuse 7 orphan 0 tw 1 alloc 9 mem 49\n", "20 50 100\n")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_TCP_MEMORY_PRESSURE status=OK tcp_mem_pages=49 pressure_pages=50 max_pages=100 reason=below_pressure",
        )

    def test_pressure_watermark_is_warning(self) -> None:
        result = self._run("TCP: inuse 7 mem 50\n", "20 50 100\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("status=WARNING", result.stdout)
        self.assertIn("reason=at_or_above_pressure", result.stdout)

    def test_max_watermark_is_critical(self) -> None:
        result = self._run("TCP: inuse 7 mem 100\n", "20 50 100\n")
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("reason=at_or_above_max", result.stdout)

    def test_missing_tcp_mem_is_unknown(self) -> None:
        result = self._run("TCP: inuse 7 orphan 0\n", "20 50 100\n")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_TCP_MEMORY_PRESSURE status=UNKNOWN tcp_mem_pages=NA pressure_pages=NA max_pages=NA reason=sockstat_tcp_mem_missing",
        )

    def test_duplicate_tcp_field_is_unknown(self) -> None:
        result = self._run("TCP: mem 10 mem 11\n", "20 50 100\n")
        self.assertEqual(result.returncode, 3)
        self.assertIn("reason=sockstat_tcp_record_invalid", result.stdout)

    def test_invalid_watermark_order_is_unknown(self) -> None:
        result = self._run("TCP: mem 10\n", "20 100 50\n")
        self.assertEqual(result.returncode, 3)
        self.assertIn("reason=tcp_mem_watermarks_invalid", result.stdout)

    def test_future_sockstat_fields_are_ignored(self) -> None:
        result = self._run("TCP: future 999 mem 10 another 1\n", "20 50 100\n")
        self.assertEqual(result.returncode, 0)
        self.assertIn("tcp_mem_pages=10", result.stdout)

    def test_oversized_input_is_unknown(self) -> None:
        result = self._run("TCP: mem 1\n" + ("x" * (64 * 1024)), "20 50 100\n")
        self.assertEqual(result.returncode, 3)
        self.assertIn("reason=input_too_large", result.stdout)

    def test_symlink_fixture_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            actual = root / "actual"
            actual.write_text("TCP: mem 10\n", encoding="ascii")
            sockstat = root / "sockstat"
            sockstat.symlink_to(actual)
            tcp_mem = root / "tcp_mem"
            tcp_mem.write_text("20 50 100\n", encoding="ascii")
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--sockstat",
                    str(sockstat),
                    "--tcp-mem",
                    str(tcp_mem),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 3)
        self.assertIn("reason=input_unavailable", result.stdout)


if __name__ == "__main__":
    unittest.main()
