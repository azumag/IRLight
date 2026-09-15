from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "docs" / "operations" / "tcp-connection-attempt-failure-monitoring.md"
INDEX = ROOT / "docs" / "operations" / "README.md"


class TcpAttemptFailRunbookTest(unittest.TestCase):
    def test_runbook_is_linked_from_operations_index(self) -> None:
        text = INDEX.read_text(encoding="utf-8")
        marker = "](tcp-connection-attempt-failure-monitoring.md)"
        self.assertEqual(text.count(marker), 1)

    def test_runbook_keeps_read_only_baseline_contract(self) -> None:
        text = RUNBOOK.read_text(encoding="utf-8")

        self.assertIn("IRLIGHT_TCP_SNMP_BASELINE_PATH", text)
        self.assertIn("IRLIGHT_TCP_SNMP_PATH", text)
        self.assertIn("IRLIGHT_TCP_ATTEMPT_FAILS_MODE=enabled", text)
        self.assertIn("tcp_attempt_fails_status=UNKNOWN", text)
        self.assertIn("同じ host", text)
        self.assertIn("network namespace", text)
        self.assertIn("WARNING", text)
        self.assertIn("単独では `CRITICAL`", text)
        self.assertIn("baseline を作成・更新・削除", text)
        self.assertIn("host / network namespace 全体の signal", text)
        self.assertIn("MaxConn=-1", text)
        self.assertIn(
            "interface_errors_status` → `udp_snmp_errors_status` → `tcp_snmp_retransmits_status` → `tcp_listen_pressure_status` → `tcp_established_resets_status` → `tcp_attempt_fails_status",
            text,
        )


if __name__ == "__main__":
    unittest.main()
