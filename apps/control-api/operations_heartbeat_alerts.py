"""Read-only dry-run evaluator for the Issue #11 delayed heartbeat warning.

The evaluator reuses the canonical read-only Node heartbeat inspector and maps
its stale-node result to the operations alert catalog.  It emits only aggregate
counts and repository-managed alert IDs; it never emits Node/Session identifiers,
provider metadata, credentials, or raw authority records.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

from node_heartbeat_inspect_cli import (
    NodeHeartbeatInspectError,
    _positive_finite,
    _read_node_authority,
    summarize_node_heartbeats,
)
from operations_alert_catalog import OperationsAlertCatalogError, load_catalog


_ALERT_ID = "NODE_HEARTBEAT_DELAYED"
_SIGNAL = "media_nodes.heartbeat_age_seconds"
_THRESHOLD_REF = "NODE_HEARTBEAT_GRACE_SECONDS"
_RUNBOOK = "docs/operations/media-node-heartbeat-stopped.md"


class OperationsHeartbeatAlertError(ValueError):
    """Raised when the heartbeat-alert contract cannot be trusted."""


def _validate_heartbeat_alert_contract(catalog: dict[str, Any]) -> None:
    alerts = catalog.get("alerts")
    if not isinstance(alerts, list):
        raise OperationsHeartbeatAlertError("catalog alerts are unavailable")

    matches = [
        alert
        for alert in alerts
        if isinstance(alert, dict) and alert.get("id") == _ALERT_ID
    ]
    if len(matches) != 1:
        raise OperationsHeartbeatAlertError("heartbeat alert contract is unavailable")

    alert = matches[0]
    if (
        alert.get("severity") != "warning"
        or alert.get("signal") != _SIGNAL
        or alert.get("trigger")
        != {"mode": "threshold", "threshold_ref": _THRESHOLD_REF}
        or alert.get("runbook") != _RUNBOOK
    ):
        raise OperationsHeartbeatAlertError(
            "heartbeat alert contract does not match evaluator"
        )


def evaluate_authority(
    authority: dict[str, Any],
    catalog: dict[str, Any],
    *,
    now: float,
    grace_seconds: float,
) -> dict[str, Any]:
    """Evaluate delayed expected heartbeats and return only aggregate output."""
    _validate_heartbeat_alert_contract(catalog)
    summary = summarize_node_heartbeats(
        authority,
        now=now,
        grace_seconds=grace_seconds,
    )
    matches = summary["stale_count"]
    return {
        "status": "MATCHED" if matches else "NO_MATCHES",
        "inspected_nodes": summary["node_count"],
        "expected_running_nodes": summary["expected_running_count"],
        "matched_alerts": {_ALERT_ID: matches} if matches else {},
        "violations": {},
    }


def _exit_code(status: str) -> int:
    return {
        "NO_MATCHES": 0,
        "MATCHED": 0,
        "UNAVAILABLE": 3,
        "INVALID_CATALOG": 4,
    }[status]


def _print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Dry-run the Issue #11 delayed Media Node heartbeat warning from "
            "read-only Node authority."
        )
    )
    parser.add_argument(
        "--node-state-dir",
        default=os.getenv("NODE_STATE_DIR", os.getenv("STATE_DIR", "/state")),
        help="Node authority directory (default: NODE_STATE_DIR, STATE_DIR, or /state)",
    )
    parser.add_argument(
        "--heartbeat-grace-seconds",
        type=_positive_finite,
        default=os.getenv("NODE_HEARTBEAT_GRACE_SECONDS", "120"),
        help="Age at which an expected heartbeat is stale (default: env or 120)",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=Path("config/operations-alert-catalog.json"),
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)

    try:
        catalog = load_catalog(args.catalog, repo_root=args.repo_root)
        _validate_heartbeat_alert_contract(catalog)
    except (OperationsAlertCatalogError, OperationsHeartbeatAlertError):
        result = {
            "status": "INVALID_CATALOG",
            "inspected_nodes": 0,
            "expected_running_nodes": 0,
            "matched_alerts": {},
            "violations": {},
        }
        _print(result)
        return _exit_code(result["status"])

    try:
        authority = _read_node_authority(Path(args.node_state_dir))
    except NodeHeartbeatInspectError:
        result = {
            "status": "UNAVAILABLE",
            "inspected_nodes": 0,
            "expected_running_nodes": 0,
            "matched_alerts": {},
            "violations": {"NODE_AUTHORITY_UNAVAILABLE": 1},
        }
        _print(result)
        return _exit_code(result["status"])

    result = evaluate_authority(
        authority,
        catalog,
        now=time.time(),
        grace_seconds=args.heartbeat_grace_seconds,
    )
    _print(result)
    return _exit_code(result["status"])


if __name__ == "__main__":
    raise SystemExit(main())
