from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = REPO_ROOT / "docs" / "operations" / "tcp-memory-pressure-monitoring.md"
INDEX = REPO_ROOT / "docs" / "operations" / "README.md"
SCRIPT = REPO_ROOT / "scripts" / "check-host-tcp-memory-pressure.py"
WRAPPER = REPO_ROOT / "scripts" / "check-host-tcp-memory-pressure.sh"
AGGREGATE = REPO_ROOT / "scripts" / "check-host-pressure.sh"


class TcpMemoryPressureRunbookTest(unittest.TestCase):
    def test_operations_index_links_runbook_once(self) -> None:
        text = INDEX.read_text(encoding="utf-8")
        self.assertEqual(text.count("](tcp-memory-pressure-monitoring.md)"), 1)

    def test_runbook_keeps_read_only_fail_closed_contract(self) -> None:
        text = RUNBOOK.read_text(encoding="utf-8")
        self.assertIn("python3 scripts/check-host-tcp-memory-pressure.py", text)
        self.assertIn("/proc/net/sockstat", text)
        self.assertIn("/proc/sys/net/ipv4/tcp_mem", text)
        self.assertIn("`UNKNOWN` / `3`", text)
        self.assertIn("sysctl", text)
        self.assertIn("変更せず", text)
        self.assertIn("64 KiB", text)
        self.assertIn("symlink", text)
        self.assertIn("特定 Session", text)

    def test_runbook_documents_default_off_aggregate_opt_in(self) -> None:
        text = RUNBOOK.read_text(encoding="utf-8")
        self.assertIn("IRLIGHT_HOST_TCP_MEMORY_MODE=enabled", text)
        self.assertIn("tcp_memory_status", text)
        self.assertIn("既定", text)
        self.assertIn("CRITICAL > UNKNOWN > WARNING > OK", text)

    def test_script_uses_kernel_watermarks_without_mutation_commands(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('default="/proc/net/sockstat"', text)
        self.assertIn('default="/proc/sys/net/ipv4/tcp_mem"', text)
        self.assertIn("pressure_pages", text)
        self.assertIn("high_pages", text)
        for forbidden in ("sysctl -w", "subprocess.run", "os.system", "socket.socket"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, text)

    def test_shell_adapter_and_aggregate_remain_read_only(self) -> None:
        wrapper = WRAPPER.read_text(encoding="utf-8")
        aggregate = AGGREGATE.read_text(encoding="utf-8")
        self.assertIn("check-host-tcp-memory-pressure.py", wrapper)
        self.assertIn("IRLIGHT_HOST_TCP_MEMORY_MODE", aggregate)
        self.assertIn('add_component "tcp_memory"', aggregate)
        for text in (wrapper, aggregate):
            for forbidden in ("sysctl -w", "docker restart", "ip route replace"):
                with self.subTest(forbidden=forbidden):
                    self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
