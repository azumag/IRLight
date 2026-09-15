from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNBOOK_INDEX = "docs/operations/README.md"

REQUIRED_RUNBOOKS = {
    "media node heartbeat": "docs/operations/media-node-heartbeat-stopped.md",
    "session process crash loop": "docs/operations/session-process-crash-loop.md",
    "widespread egress failure": "docs/operations/egress-widespread-failure.md",
    "ingest unavailable": "docs/operations/ingest-connectivity-failure.md",
    "control plane unavailable": "docs/operations/control-plane-unavailable.md",
    "database or redis unavailable": "docs/operations/datastore-unavailable.md",
    "object storage unavailable": "docs/operations/object-storage-unavailable.md",
    "tls certificate update failure": "docs/operations/rtmps-certificate-update-failure.md",
    "media node capacity exhausted": "docs/operations/media-node-capacity-high.md",
    "secret exposure suspected": "docs/operations/secret-exposure-suspected.md",
    "billing webhook stalled": "docs/operations/billing-webhook-stalled.md",
    "emergency abuse stop": "docs/operations/emergency-abuse-stop.md",
}

RELATED_PROCEDURES = {
    "deploy rollback": "docs/operations/deploy-rollback.md",
    "production deploy preflight": "docs/production-deploy-preflight.md",
    "state readiness": "docs/operations/state-readiness.md",
    "state restore drill": "docs/operations/state-restore-drill.md",
    "state provider reconciliation": "docs/operations/state-provider-reconciliation.md",
    "destination verification admission": "docs/operations/destination-verification-admission.md",
    "auth kdf admission": "docs/operations/auth-kdf-admission.md",
    "media node resource pressure": "docs/operations/media-node-resource-pressure.md",
    "resource pressure check contract": "docs/operations/resource-pressure-check-contract.md",
    "cgroup runtime pressure aggregate": "docs/operations/cgroup-runtime-pressure-aggregate.md",
    "auth session gc": "docs/operations/auth-session-gc.md",
    "user session capacity": "docs/operations/session-capacity-exhaustion.md",
    "log redaction audit": "docs/operations/log-redaction-audit.md",
    "alert catalog": "docs/operations/alert-catalog.md",
    "event alert dry-run": "docs/operations/event-alert-dry-run.md",
    "media node availability alert dry-run": "docs/operations/media-node-availability-alert-dry-run.md",
    "node heartbeat alert dry-run": "docs/operations/node-heartbeat-alert-dry-run.md",
    "media node capacity alert dry-run": "docs/operations/media-node-capacity-alert-dry-run.md",
    "node resource pressure alert dry-run": "docs/operations/node-resource-pressure-alert-dry-run.md",
    "session process crash-loop alert dry-run": "docs/operations/session-process-crash-loop-alert-dry-run.md",
    "session failure surge alert dry-run": "docs/operations/session-failure-surge-alert-dry-run.md",
    "egress failure surge alert dry-run": "docs/operations/egress-failure-surge-alert-dry-run.md",
    "egress reconnect rate alert dry-run": "docs/operations/egress-reconnect-rate-alert-dry-run.md",
    "ingest unavailable alert dry-run": "docs/operations/ingest-unavailable-alert-dry-run.md",
    "asset failure rate alert dry-run": "docs/operations/asset-failure-rate-alert-dry-run.md",
    "billing webhook alert dry-run": "docs/operations/billing-webhook-alert-dry-run.md",
    "targeted IPv6 default route diagnostics": "docs/operations/targeted-ipv6-default-route-diagnostics.md",
    "targeted network egress health": "docs/operations/targeted-network-egress-health.md",
    "network interface error monitoring": "docs/operations/network-interface-error-monitoring.md",
    "udp snmp error monitoring": "docs/operations/udp-snmp-error-monitoring.md",
    "tcp snmp retransmit monitoring": "docs/operations/tcp-snmp-retransmit-monitoring.md",
}

NEW_RUNBOOKS = {
    "docs/operations/datastore-unavailable.md",
    "docs/operations/object-storage-unavailable.md",
    "docs/operations/billing-webhook-stalled.md",
    "docs/operations/emergency-abuse-stop.md",
    "docs/operations/media-node-capacity-high.md",
    "docs/operations/media-node-resource-pressure.md",
}


class OperationsRunbookInventoryTests(unittest.TestCase):
    def test_issue_11_required_runbooks_exist(self) -> None:
        for scenario, relative_path in REQUIRED_RUNBOOKS.items():
            with self.subTest(scenario=scenario):
                path = REPO_ROOT / relative_path
                self.assertTrue(path.is_file(), f"missing runbook: {relative_path}")
                text = path.read_text(encoding="utf-8")
                self.assertTrue(text.startswith("# "), f"runbook must start with an H1: {relative_path}")
                self.assertGreater(len(text.strip()), 200, f"runbook is unexpectedly empty: {relative_path}")

    def test_issue_11_required_runbooks_are_linked_from_index(self) -> None:
        index_path = REPO_ROOT / RUNBOOK_INDEX
        self.assertTrue(index_path.is_file(), f"missing runbook index: {RUNBOOK_INDEX}")
        text = index_path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# "), "runbook index must start with an H1")
        self.assertIn("## 共通安全原則", text)
        self.assertIn("## Issue #11 必須 runbook", text)

        for scenario, relative_path in REQUIRED_RUNBOOKS.items():
            with self.subTest(scenario=scenario):
                link_target = Path(relative_path).name
                link_marker = f"]({link_target})"
                self.assertEqual(
                    text.count(link_marker),
                    1,
                    f"runbook index must link exactly once to {relative_path}",
                )

    def test_related_operations_procedures_are_linked_from_index(self) -> None:
        index_path = REPO_ROOT / RUNBOOK_INDEX
        text = index_path.read_text(encoding="utf-8")
        self.assertIn("## 関連運用手順", text)

        for procedure, relative_path in RELATED_PROCEDURES.items():
            with self.subTest(procedure=procedure):
                path = REPO_ROOT / relative_path
                self.assertTrue(path.is_file(), f"missing operations procedure: {relative_path}")
                link_target = Path(relative_path).name
                link_marker = f"]({link_target})" if path.parent.name == "operations" else f"](../{link_target})"
                self.assertEqual(
                    text.count(link_marker),
                    1,
                    f"operations index must link exactly once to {relative_path}",
                )

    def test_network_egress_runbook_keeps_interface_error_opt_in_contract(self) -> None:
        relative_path = RELATED_PROCEDURES["targeted network egress health"]
        text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")

        self.assertIn("IRLIGHT_NETWORK_INTERFACE_ERRORS_MODE=enabled", text)
        self.assertIn("IRLIGHT_NETWORK_STATS_BASELINE_DIR", text)
        self.assertIn("既定では NIC の累積 error/drop counter は aggregate に含めません", text)
        self.assertIn("interface_errors_status=UNKNOWN", text)
        self.assertIn("aggregate 自身は作成・更新・削除しません", text)

    def test_network_egress_runbook_keeps_udp_snmp_opt_in_contract(self) -> None:
        relative_path = RELATED_PROCEDURES["targeted network egress health"]
        text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")

        self.assertIn("IRLIGHT_UDP_SNMP_ERRORS_MODE=enabled", text)
        self.assertIn("IRLIGHT_UDP_SNMP_BASELINE_PATH", text)
        self.assertIn("既定では `/proc/net/snmp` の UDP 累積 error counter を aggregate に含めません", text)
        self.assertIn("udp_snmp_errors_status=UNKNOWN", text)
        self.assertIn("host / network namespace 全体の signal", text)
        self.assertIn("aggregate 自身は作成・更新・削除しません", text)

    def test_network_egress_runbook_keeps_tcp_snmp_opt_in_contract(self) -> None:
        relative_path = RELATED_PROCEDURES["targeted network egress health"]
        text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")

        self.assertIn("IRLIGHT_TCP_SNMP_RETRANSMITS_MODE=enabled", text)
        self.assertIn("IRLIGHT_TCP_SNMP_BASELINE_PATH", text)
        self.assertIn("IRLIGHT_TCP_SNMP_PATH", text)
        self.assertIn("tcp_snmp_retransmits_status=UNKNOWN", text)
        self.assertIn("host / network namespace 全体の signal", text)
        self.assertIn("aggregate 自身は baseline を作成・更新・削除しません", text)

    def test_network_egress_runbook_keeps_tcp_listener_pressure_opt_in_contract(self) -> None:
        relative_path = RELATED_PROCEDURES["targeted network egress health"]
        text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")

        self.assertIn("IRLIGHT_TCP_LISTEN_PRESSURE_MODE=enabled", text)
        self.assertIn("IRLIGHT_TCP_NETSTAT_BASELINE_PATH", text)
        self.assertIn("IRLIGHT_TCP_NETSTAT_PATH", text)
        self.assertIn("tcp_listen_pressure_status=UNKNOWN", text)
        self.assertIn("host / network namespace 全体の signal", text)
        self.assertIn("aggregate 自身は baseline を作成・更新・削除しません", text)
        self.assertIn(
            "interface_errors_status` → `udp_snmp_errors_status` → `tcp_snmp_retransmits_status` → `tcp_listen_pressure_status",
            text,
        )

    def test_tcp_snmp_runbook_keeps_read_only_baseline_contract(self) -> None:
        relative_path = RELATED_PROCEDURES["tcp snmp retransmit monitoring"]
        text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")

        self.assertIn("IRLIGHT_TCP_SNMP_BASELINE_PATH", text)
        self.assertIn("IRLIGHT_TCP_SNMP_PATH", text)
        self.assertIn("同じ host", text)
        self.assertIn("network namespace", text)
        self.assertIn("WARNING", text)
        self.assertIn("単独では `CRITICAL`", text)
        self.assertIn("baseline を作成・更新・削除", text)

    def test_cgroup_runtime_runbook_keeps_cpu_throttling_opt_in_contract(self) -> None:
        relative_path = RELATED_PROCEDURES["cgroup runtime pressure aggregate"]
        text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")

        self.assertIn("IRLIGHT_CGROUP_CPU_STAT_BASELINE_PATH", text)
        self.assertIn("cpu_throttling_status=<status>", text)
        self.assertIn("checker 単独では `CRITICAL` にしない", text)
        self.assertIn("既存 stdout / exit code 形式を変更しない", text)
        self.assertIn("baseline を作成・更新・削除しない", text)

    def test_deploy_rollback_runbook_keeps_safe_decision_boundaries(self) -> None:
        relative_path = RELATED_PROCEDURES["deploy rollback"]
        text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")

        for heading in (
            "## Deploy 前確認",
            "## Deploy 実施条件",
            "## Deploy 後確認",
            "## Rollback 判定",
            "## Rollback 手順",
            "## 復旧確認",
            "## 事後作業",
        ):
            with self.subTest(heading=heading):
                self.assertIn(heading, text)

        self.assertIn("docs/production-deploy-preflight.md", text)
        self.assertIn("state-readiness.md", text)
        self.assertIn("state-restore-drill.md", text)
        self.assertIn(
            "`docker compose down -v`、volume prune、state volume 削除、marker 削除、空 state 作成を rollback 手段にしない。",
            text,
        )
        self.assertIn(
            "この runbook は本番の `pull` / `up` / `restart` を自動化しません。",
            text,
        )

    def test_new_runbooks_have_actionable_lifecycle_sections(self) -> None:
        for relative_path in NEW_RUNBOOKS:
            with self.subTest(path=relative_path):
                text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
                self.assertIn("## 検知", text)
                self.assertIn("## 影響判定", text)
                self.assertIn("## 事後作業", text)


if __name__ == "__main__":
    unittest.main()
