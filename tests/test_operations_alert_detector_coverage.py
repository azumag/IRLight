from __future__ import annotations

import json
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = REPO_ROOT / "config" / "operations-alert-catalog.json"
ALERT_CATALOG_DOC = REPO_ROOT / "docs" / "operations" / "alert-catalog.md"
EVENT_EVALUATOR = "apps/control-api/operations_event_alerts.py"
EVENT_DRY_RUN_DOC = "docs/operations/event-alert-dry-run.md"

THRESHOLD_DRY_RUNS = {
    "MEDIA_NODES_ALL_UNAVAILABLE": (
        "apps/control-api/operations_node_availability_alerts.py",
        "docs/operations/media-node-availability-alert-dry-run.md",
    ),
    "NODE_HEARTBEAT_DELAYED": (
        "apps/control-api/operations_heartbeat_alerts.py",
        "docs/operations/node-heartbeat-alert-dry-run.md",
    ),
    "NODE_RESOURCE_PRESSURE": (
        "apps/control-api/operations_node_resource_pressure_alerts.py",
        "docs/operations/node-resource-pressure-alert-dry-run.md",
    ),
    "SESSION_PROCESS_CRASH_LOOP": (
        "apps/control-api/operations_session_restart_alerts.py",
        "docs/operations/session-process-crash-loop-alert-dry-run.md",
    ),
    "SESSION_FAILURE_SURGE": (
        "apps/control-api/operations_session_failure_alerts.py",
        "docs/operations/session-failure-surge-alert-dry-run.md",
    ),
    "MEDIA_NODE_CAPACITY_HIGH": (
        "apps/control-api/operations_capacity_alerts.py",
        "docs/operations/media-node-capacity-alert-dry-run.md",
    ),
    "EGRESS_FAILURE_SURGE": (
        "apps/control-api/operations_egress_failure_alerts.py",
        "docs/operations/egress-failure-surge-alert-dry-run.md",
    ),
    "RECONNECT_RATE_HIGH": (
        "apps/control-api/operations_egress_reconnect_alerts.py",
        "docs/operations/egress-reconnect-rate-alert-dry-run.md",
    ),
    "INGEST_UNAVAILABLE": (
        "apps/control-api/operations_ingest_unavailable_alerts.py",
        "docs/operations/ingest-unavailable-alert-dry-run.md",
    ),
    "ASSET_FAILURE_RATE_HIGH": (
        "apps/control-api/operations_asset_failure_alerts.py",
        "docs/operations/asset-failure-rate-alert-dry-run.md",
    ),
    "BILLING_WEBHOOK_DELAYED": (
        "apps/control-api/operations_billing_webhook_alerts.py",
        "docs/operations/billing-webhook-alert-dry-run.md",
    ),
}


class OperationsAlertDetectorCoverageTests(unittest.TestCase):
    def load_catalog(self) -> dict[str, object]:
        return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))

    def test_every_threshold_alert_has_explicit_dry_run_coverage(self) -> None:
        catalog = self.load_catalog()
        threshold_alerts = {
            alert["id"]
            for alert in catalog["alerts"]
            if alert["trigger"]["mode"] == "threshold"
        }

        self.assertEqual(threshold_alerts, set(THRESHOLD_DRY_RUNS))
        for alert_id, (evaluator, dry_run_doc) in THRESHOLD_DRY_RUNS.items():
            with self.subTest(alert_id=alert_id):
                self.assertTrue(
                    (REPO_ROOT / evaluator).is_file(),
                    f"missing dry-run evaluator for {alert_id}: {evaluator}",
                )
                self.assertTrue(
                    (REPO_ROOT / dry_run_doc).is_file(),
                    f"missing dry-run documentation for {alert_id}: {dry_run_doc}",
                )

    def test_event_alerts_keep_generic_dry_run_coverage(self) -> None:
        catalog = self.load_catalog()
        event_alerts = [
            alert
            for alert in catalog["alerts"]
            if alert["trigger"]["mode"] == "event"
        ]

        self.assertTrue(event_alerts, "catalog unexpectedly has no event alerts")
        self.assertTrue((REPO_ROOT / EVENT_EVALUATOR).is_file())
        self.assertTrue((REPO_ROOT / EVENT_DRY_RUN_DOC).is_file())

    def test_alert_catalog_doc_lists_every_threshold_dry_run(self) -> None:
        text = ALERT_CATALOG_DOC.read_text(encoding="utf-8")

        for alert_id, (evaluator, dry_run_doc) in THRESHOLD_DRY_RUNS.items():
            with self.subTest(alert_id=alert_id):
                self.assertIn(f"`{alert_id}`", text)
                self.assertIn(f"`{Path(evaluator).name}`", text)
                self.assertIn(f"`{Path(dry_run_doc).name}`", text)


if __name__ == "__main__":
    unittest.main()
