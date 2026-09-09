from __future__ import annotations

import copy
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTROL_API_ROOT = REPO_ROOT / "apps" / "control-api"
MODULE_PATH = CONTROL_API_ROOT / "operations_alert_catalog.py"
CATALOG_PATH = REPO_ROOT / "config" / "operations-alert-catalog.json"

if str(CONTROL_API_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROL_API_ROOT))

spec = importlib.util.spec_from_file_location("operations_alert_catalog", MODULE_PATH)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

REQUIRED_RUNBOOKS = {
    "docs/operations/media-node-heartbeat-stopped.md",
    "docs/operations/session-process-crash-loop.md",
    "docs/operations/egress-widespread-failure.md",
    "docs/operations/ingest-connectivity-failure.md",
    "docs/operations/control-plane-unavailable.md",
    "docs/operations/datastore-unavailable.md",
    "docs/operations/object-storage-unavailable.md",
    "docs/operations/rtmps-certificate-update-failure.md",
    "docs/operations/session-capacity-exhaustion.md",
    "docs/operations/secret-exposure-suspected.md",
    "docs/operations/billing-webhook-stalled.md",
    "docs/operations/emergency-abuse-stop.md",
}

REQUIRED_CRITICAL_ALERTS = {
    "MEDIA_NODES_ALL_UNAVAILABLE",
    "SESSION_FAILURE_SURGE",
    "SECRET_EXPOSURE_SUSPECTED",
    "DATASTORE_UNAVAILABLE",
    "EGRESS_FAILURE_SURGE",
    "NODE_RESOURCE_EXHAUSTED",
}

REQUIRED_WARNING_ALERTS = {
    "SESSION_CAPACITY_HIGH",
    "RECONNECT_RATE_HIGH",
    "ASSET_FAILURE_RATE_HIGH",
    "BILLING_WEBHOOK_DELAYED",
    "NODE_HEARTBEAT_DELAYED",
    "NODE_RESOURCE_PRESSURE",
}


class OperationsAlertCatalogTests(unittest.TestCase):
    def load_real_catalog(self) -> dict[str, object]:
        return module.load_catalog(CATALOG_PATH, repo_root=REPO_ROOT)

    def test_real_catalog_is_valid_and_all_required_runbooks_are_routable(self) -> None:
        catalog = self.load_real_catalog()
        alerts = catalog["alerts"]
        self.assertIsInstance(alerts, list)
        runbooks = {alert["runbook"] for alert in alerts}
        self.assertTrue(REQUIRED_RUNBOOKS.issubset(runbooks))

    def test_issue_11_critical_and_warning_alerts_are_catalogued(self) -> None:
        catalog = self.load_real_catalog()
        alerts = {alert["id"]: alert for alert in catalog["alerts"]}

        for alert_id in REQUIRED_CRITICAL_ALERTS:
            with self.subTest(alert_id=alert_id):
                self.assertIn(alert_id, alerts)
                self.assertEqual(alerts[alert_id]["severity"], "critical")

        for alert_id in REQUIRED_WARNING_ALERTS:
            with self.subTest(alert_id=alert_id):
                self.assertIn(alert_id, alerts)
                self.assertEqual(alerts[alert_id]["severity"], "warning")

    def test_every_alert_has_recovery_notification_and_safe_dedup_keys(self) -> None:
        catalog = self.load_real_catalog()
        for alert in catalog["alerts"]:
            with self.subTest(alert_id=alert["id"]):
                self.assertIs(alert["recovery_notification"], True)
                self.assertTrue(set(alert["dedup_keys"]).issubset(module.SAFE_DEDUP_KEYS))

    def test_non_integer_schema_versions_are_rejected(self) -> None:
        for schema_version in (True, 1.0, "1"):
            with self.subTest(schema_version=schema_version):
                catalog = copy.deepcopy(self.load_real_catalog())
                catalog["schema_version"] = schema_version
                with self.assertRaises(module.OperationsAlertCatalogError):
                    module.validate_catalog(catalog, repo_root=REPO_ROOT)

    def test_duplicate_alert_id_is_rejected(self) -> None:
        catalog = copy.deepcopy(self.load_real_catalog())
        catalog["alerts"].append(copy.deepcopy(catalog["alerts"][0]))
        with self.assertRaises(module.OperationsAlertCatalogError):
            module.validate_catalog(catalog, repo_root=REPO_ROOT)

    def test_duplicate_event_type_is_rejected(self) -> None:
        catalog = copy.deepcopy(self.load_real_catalog())
        first_event = next(
            alert
            for alert in catalog["alerts"]
            if alert["trigger"]["mode"] == "event"
        )
        duplicate = copy.deepcopy(first_event)
        duplicate["id"] = "SECOND_ALERT_FOR_SAME_EVENT"
        catalog["alerts"].append(duplicate)
        with self.assertRaisesRegex(
            module.OperationsAlertCatalogError, "duplicate event_type"
        ):
            module.validate_catalog(catalog, repo_root=REPO_ROOT)

    def test_unhashable_or_secret_like_dedup_keys_fail_closed(self) -> None:
        for dedup_keys in ([{"token": "value"}], ["environment", "token"]):
            with self.subTest(dedup_keys=dedup_keys):
                catalog = copy.deepcopy(self.load_real_catalog())
                catalog["alerts"][0]["dedup_keys"] = dedup_keys
                with self.assertRaises(module.OperationsAlertCatalogError):
                    module.validate_catalog(catalog, repo_root=REPO_ROOT)

    def test_alert_without_recovery_notification_is_rejected(self) -> None:
        catalog = copy.deepcopy(self.load_real_catalog())
        catalog["alerts"][0]["recovery_notification"] = False
        with self.assertRaises(module.OperationsAlertCatalogError):
            module.validate_catalog(catalog, repo_root=REPO_ROOT)

    def test_runbook_path_escape_is_rejected(self) -> None:
        catalog = copy.deepcopy(self.load_real_catalog())
        catalog["alerts"][0]["runbook"] = "docs/operations/../../secrets/example.md"
        with self.assertRaises(module.OperationsAlertCatalogError):
            module.validate_catalog(catalog, repo_root=REPO_ROOT)

    def test_duplicate_json_key_and_non_finite_number_are_rejected(self) -> None:
        invalid_documents = (
            '{"schema_version":1,"schema_version":1,"alerts":[]}',
            '{"schema_version":1,"alerts":[],"invalid":NaN}',
        )
        for document in invalid_documents:
            with self.subTest(document=document):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "catalog.json"
                    path.write_text(document, encoding="utf-8")
                    with self.assertRaises(module.OperationsAlertCatalogError):
                        module.load_catalog(path)


if __name__ == "__main__":
    unittest.main()
