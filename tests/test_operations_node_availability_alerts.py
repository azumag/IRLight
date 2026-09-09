from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
CONTROL_API_ROOT = REPO_ROOT / "apps" / "control-api"
MODULE_PATH = CONTROL_API_ROOT / "operations_node_availability_alerts.py"
CATALOG_PATH = REPO_ROOT / "config" / "operations-alert-catalog.json"

if str(CONTROL_API_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROL_API_ROOT))

spec = importlib.util.spec_from_file_location(
    "operations_node_availability_alerts", MODULE_PATH
)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

from state_safety import initialization_marker  # noqa: E402


def node_record(
    node_id: str,
    *,
    created_at: float = 800.0,
    last_heartbeat_at: float | None = 950.0,
    status: str = "READY",
    desired_state: str = "RUNNING",
) -> dict[str, object]:
    return {
        "node_id": node_id,
        "session_id": f"session-{node_id}",
        "provider_server_id": f"provider-secretish-{node_id}",
        "boot_id": f"boot-secretish-{node_id}",
        "agent_version": "test-agent",
        "access_token_sha256": "a" * 64,
        "status": status,
        "desired_state": desired_state,
        "absolute_deadline": 2000.0,
        "created_at": created_at,
        "last_heartbeat_at": last_heartbeat_at,
    }


def authority(*records: dict[str, object]) -> dict[str, object]:
    return {
        "nodes": {record["node_id"]: record for record in records},
        "next_node_seq": len(records) + 1,
        "tokens": {},
    }


class OperationsNodeAvailabilityAlertTests(unittest.TestCase):
    def load_catalog(self) -> dict[str, object]:
        return module.load_catalog(CATALOG_PATH, repo_root=REPO_ROOT)

    def test_fresh_ready_node_keeps_all_unavailable_alert_clear(self) -> None:
        result = module.evaluate_authority(
            authority(node_record("node-1", last_heartbeat_at=950.0)),
            self.load_catalog(),
            now=1000.0,
            grace_seconds=120.0,
        )

        self.assertEqual(result["status"], "NO_MATCHES")
        self.assertEqual(result["inspected_nodes"], 1)
        self.assertEqual(result["available_nodes"], 1)
        self.assertEqual(result["matched_alerts"], {})
        self.assertEqual(result["violations"], {})

    def test_stale_ready_and_stopped_nodes_match_all_unavailable(self) -> None:
        result = module.evaluate_authority(
            authority(
                node_record("node-1", last_heartbeat_at=800.0),
                node_record(
                    "node-2",
                    last_heartbeat_at=950.0,
                    status="STOPPED",
                    desired_state="STOPPED",
                ),
            ),
            self.load_catalog(),
            now=1000.0,
            grace_seconds=120.0,
        )

        self.assertEqual(result["status"], "MATCHED")
        self.assertEqual(result["inspected_nodes"], 2)
        self.assertEqual(result["available_nodes"], 0)
        self.assertEqual(
            result["matched_alerts"], {"MEDIA_NODES_ALL_UNAVAILABLE": 1}
        )

    def test_bootstrapping_node_is_not_counted_as_available(self) -> None:
        result = module.evaluate_authority(
            authority(
                node_record(
                    "node-1",
                    last_heartbeat_at=950.0,
                    status="BOOTSTRAPPING",
                )
            ),
            self.load_catalog(),
            now=1000.0,
            grace_seconds=120.0,
        )

        self.assertEqual(result["status"], "MATCHED")
        self.assertEqual(result["available_nodes"], 0)

    def test_empty_valid_authority_matches_zero_available_nodes(self) -> None:
        result = module.evaluate_authority(
            authority(),
            self.load_catalog(),
            now=1000.0,
            grace_seconds=120.0,
        )

        self.assertEqual(result["status"], "MATCHED")
        self.assertEqual(result["inspected_nodes"], 0)
        self.assertEqual(result["available_nodes"], 0)
        self.assertEqual(
            result["matched_alerts"], {"MEDIA_NODES_ALL_UNAVAILABLE": 1}
        )

    def test_catalog_contract_drift_is_rejected(self) -> None:
        for field, value in (
            ("signal", "media_nodes.ready_count"),
            ("severity", "warning"),
            ("runbook", "docs/operations/control-plane-unavailable.md"),
        ):
            with self.subTest(field=field):
                catalog = copy.deepcopy(self.load_catalog())
                alert = next(
                    candidate
                    for candidate in catalog["alerts"]
                    if candidate["id"] == "MEDIA_NODES_ALL_UNAVAILABLE"
                )
                alert[field] = value
                with self.assertRaises(
                    module.OperationsNodeAvailabilityAlertError
                ):
                    module.evaluate_authority(
                        authority(node_record("node-1")),
                        catalog,
                        now=1000.0,
                        grace_seconds=120.0,
                    )

    def test_cli_aggregates_without_exposing_node_session_or_provider_identifiers(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="irlight-node-availability-alert-"
        ) as directory:
            root = Path(directory)
            nodes_path = root / "nodes.json"
            nodes_path.write_text(
                json.dumps(
                    authority(
                        node_record("node-sensitive", last_heartbeat_at=800.0)
                    ),
                    allow_nan=False,
                ),
                encoding="utf-8",
            )
            initialization_marker(nodes_path).write_text("v1\n", encoding="utf-8")
            before = {
                path.name: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in root.iterdir()
                if path.is_file()
            }
            output = io.StringIO()

            with patch(
                "operations_node_availability_alerts.time.time", return_value=1000.0
            ):
                with contextlib.redirect_stdout(output):
                    exit_code = module.main(
                        [
                            "--node-state-dir",
                            str(root),
                            "--heartbeat-grace-seconds",
                            "120",
                            "--catalog",
                            str(CATALOG_PATH),
                            "--repo-root",
                            str(REPO_ROOT),
                        ]
                    )

            self.assertEqual(exit_code, 0)
            rendered = output.getvalue()
            result = json.loads(rendered)
            self.assertEqual(
                result["matched_alerts"], {"MEDIA_NODES_ALL_UNAVAILABLE": 1}
            )
            self.assertNotIn("node-sensitive", rendered)
            self.assertNotIn("session-node-sensitive", rendered)
            self.assertNotIn("provider-secretish", rendered)
            after = {
                path.name: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in root.iterdir()
                if path.is_file()
            }
            self.assertEqual(after, before)

    def test_missing_authority_fails_closed_without_partial_alert(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="irlight-node-availability-alert-missing-"
        ) as directory:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exit_code = module.main(
                    [
                        "--node-state-dir",
                        directory,
                        "--catalog",
                        str(CATALOG_PATH),
                        "--repo-root",
                        str(REPO_ROOT),
                    ]
                )

            result = json.loads(output.getvalue())
            self.assertEqual(exit_code, 3)
            self.assertEqual(result["status"], "UNAVAILABLE")
            self.assertEqual(result["matched_alerts"], {})
            self.assertEqual(
                result["violations"], {"NODE_AUTHORITY_UNAVAILABLE": 1}
            )


if __name__ == "__main__":
    unittest.main()
