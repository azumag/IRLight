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
    "auth session gc": "docs/operations/auth-session-gc.md",
    "user session capacity": "docs/operations/session-capacity-exhaustion.md",
    "log redaction audit": "docs/operations/log-redaction-audit.md",
    "alert catalog": "docs/operations/alert-catalog.md",
    "event alert dry-run": "docs/operations/event-alert-dry-run.md",
    "media node availability alert dry-run": "docs/operations/media-node-availability-alert-dry-run.md",
    "node heartbeat alert dry-run": "docs/operations/node-heartbeat-alert-dry-run.md",
    "media node capacity alert dry-run": "docs/operations/media-node-capacity-alert-dry-run.md",
    "session process crash-loop alert dry-run": "docs/operations/session-process-crash-loop-alert-dry-run.md",
    "session failure surge alert dry-run": "docs/operations/session-failure-surge-alert-dry-run.md",
    "egress failure surge alert dry-run": "docs/operations/egress-failure-surge-alert-dry-run.md",
    "egress reconnect rate alert dry-run": "docs/operations/egress-reconnect-rate-alert-dry-run.md",
    "ingest unavailable alert dry-run": "docs/operations/ingest-unavailable-alert-dry-run.md",
    "asset failure rate alert dry-run": "docs/operations/asset-failure-rate-alert-dry-run.md",
    "billing webhook alert dry-run": "docs/operations/billing-webhook-alert-dry-run.md",
}

NEW_RUNBOOKS = {
    "docs/operations/datastore-unavailable.md",
    "docs/operations/object-storage-unavailable.md",
    "docs/operations/billing-webhook-stalled.md",
    "docs/operations/emergency-abuse-stop.md",
    "docs/operations/media-node-capacity-high.md",
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
