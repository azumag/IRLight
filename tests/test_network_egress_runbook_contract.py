from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = REPO_ROOT / "docs/operations/targeted-network-egress-health.md"


class NetworkEgressRunbookContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = RUNBOOK.read_text(encoding="utf-8")

    def test_documents_all_optional_modes_and_checkers(self) -> None:
        for marker in (
            "IRLIGHT_NETWORK_INTERFACE_ERRORS_MODE=enabled",
            "check-network-interface-errors.sh",
            "IRLIGHT_UDP_SNMP_ERRORS_MODE=enabled",
            "check-udp-snmp-errors.sh",
            "IRLIGHT_TCP_SNMP_RETRANSMITS_MODE=enabled",
            "check-tcp-snmp-retransmits.sh",
            "IRLIGHT_TCP_LISTEN_PRESSURE_MODE=enabled",
            "check-tcp-listen-overflows.sh",
            "IRLIGHT_TCP_ESTABLISHED_RESETS_MODE=enabled",
            "check-tcp-snmp-established-resets.sh",
            "IRLIGHT_TCP_ATTEMPT_FAILS_MODE=enabled",
            "check-tcp-snmp-attempt-fails.sh",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.text)

    def test_optional_status_fields_keep_registry_order(self) -> None:
        expected = (
            "`interface_errors_status` → `udp_snmp_errors_status` → "
            "`tcp_snmp_retransmits_status` → `tcp_listen_pressure_status` → "
            "`tcp_established_resets_status` → `tcp_attempt_fails_status`"
        )
        self.assertIn(expected, self.text)

    def test_new_tcp_signals_keep_fail_closed_read_only_contract(self) -> None:
        for marker in (
            "tcp_established_resets_status=UNKNOWN",
            "tcp_attempt_fails_status=UNKNOWN",
            "tcp-established-reset-monitoring.md",
            "tcp-connection-attempt-failure-monitoring.md",
            "aggregate 自身は baseline を作成・更新・削除しません",
            "`EstabResets` は host / network namespace 全体の signal",
            "`AttemptFails` は host / network namespace 全体の signal",
            "特定 Session / destination / service へ自動帰属しません",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.text)

    def test_severity_and_legacy_output_contract_remain_explicit(self) -> None:
        self.assertIn("`CRITICAL > UNKNOWN > WARNING > OK`", self.text)
        self.assertIn("legacy 出力を byte-for-byte 維持します", self.text)
        self.assertIn("component timeout", self.text)


if __name__ == "__main__":
    unittest.main()
