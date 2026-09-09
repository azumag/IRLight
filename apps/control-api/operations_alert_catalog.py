"""Validation for the operations alert catalog.

The catalog is deliberately routing-neutral: it defines stable alert IDs,
severity, signal ownership, runbook linkage, deduplication dimensions and the
source of each trigger threshold/event. It does not send notifications or call
providers.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

from state_safety import load_json_authority


class OperationsAlertCatalogError(ValueError):
    """Raised when the operations alert catalog cannot be trusted."""


ALLOWED_SEVERITIES = {"critical", "warning"}
ALLOWED_TRIGGER_MODES = {"event", "threshold"}
REQUIRED_ALERT_FIELDS = {
    "id",
    "severity",
    "signal",
    "trigger",
    "runbook",
    "dedup_keys",
    "recovery_notification",
}
SAFE_DEDUP_KEYS = {
    "environment",
    "region",
    "service",
    "node_id",
    "session_id",
    "reason_code",
}
_ID_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,127}$")
_SIGNAL_RE = re.compile(r"^[a-z][a-z0-9_.]{2,127}$")
_TRIGGER_REF_RE = re.compile(r"^[a-zA-Z0-9_.:-]{3,160}$")


def _require_nonempty_string(record: dict[str, Any], field: str, *, context: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value:
        raise OperationsAlertCatalogError(f"{context} has invalid {field}")
    return value


def _validate_trigger(trigger: Any, *, alert_id: str) -> None:
    if not isinstance(trigger, dict):
        raise OperationsAlertCatalogError(f"alert {alert_id} has invalid trigger")
    mode = _require_nonempty_string(trigger, "mode", context=f"alert {alert_id} trigger")
    if mode not in ALLOWED_TRIGGER_MODES:
        raise OperationsAlertCatalogError(f"alert {alert_id} has unknown trigger mode")

    if mode == "event":
        if set(trigger) != {"mode", "event_type"}:
            raise OperationsAlertCatalogError(
                f"alert {alert_id} event trigger has unexpected fields"
            )
        event_type = _require_nonempty_string(
            trigger, "event_type", context=f"alert {alert_id} trigger"
        )
        if not _ID_RE.fullmatch(event_type):
            raise OperationsAlertCatalogError(f"alert {alert_id} has invalid event_type")
        return

    if set(trigger) != {"mode", "threshold_ref"}:
        raise OperationsAlertCatalogError(
            f"alert {alert_id} threshold trigger has unexpected fields"
        )
    threshold_ref = _require_nonempty_string(
        trigger, "threshold_ref", context=f"alert {alert_id} trigger"
    )
    if not _TRIGGER_REF_RE.fullmatch(threshold_ref):
        raise OperationsAlertCatalogError(f"alert {alert_id} has invalid threshold_ref")


def _validate_runbook(runbook: str, *, repo_root: Path | None, alert_id: str) -> None:
    path = Path(runbook)
    if (
        path.is_absolute()
        or ".." in path.parts
        or path.suffix != ".md"
        or path.parts[:2] != ("docs", "operations")
    ):
        raise OperationsAlertCatalogError(f"alert {alert_id} has unsafe runbook path")
    if repo_root is not None and not (repo_root / path).is_file():
        raise OperationsAlertCatalogError(f"alert {alert_id} references missing runbook")


def validate_catalog(payload: Any, *, repo_root: Path | None = None) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "alerts"}:
        raise OperationsAlertCatalogError("catalog has invalid top-level structure")
    schema_version = payload.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != 1
    ):
        raise OperationsAlertCatalogError("catalog has unsupported schema_version")

    alerts = payload.get("alerts")
    if not isinstance(alerts, list) or not alerts:
        raise OperationsAlertCatalogError("catalog must contain alerts")

    seen_ids: set[str] = set()
    for index, alert in enumerate(alerts):
        context = f"alert[{index}]"
        if not isinstance(alert, dict) or set(alert) != REQUIRED_ALERT_FIELDS:
            raise OperationsAlertCatalogError(f"{context} has invalid fields")

        alert_id = _require_nonempty_string(alert, "id", context=context)
        if not _ID_RE.fullmatch(alert_id):
            raise OperationsAlertCatalogError(f"{context} has invalid id")
        if alert_id in seen_ids:
            raise OperationsAlertCatalogError("catalog contains duplicate alert id")
        seen_ids.add(alert_id)

        severity = _require_nonempty_string(alert, "severity", context=f"alert {alert_id}")
        if severity not in ALLOWED_SEVERITIES:
            raise OperationsAlertCatalogError(f"alert {alert_id} has invalid severity")

        signal = _require_nonempty_string(alert, "signal", context=f"alert {alert_id}")
        if not _SIGNAL_RE.fullmatch(signal):
            raise OperationsAlertCatalogError(f"alert {alert_id} has invalid signal")

        _validate_trigger(alert["trigger"], alert_id=alert_id)

        runbook = _require_nonempty_string(alert, "runbook", context=f"alert {alert_id}")
        _validate_runbook(runbook, repo_root=repo_root, alert_id=alert_id)

        dedup_keys = alert.get("dedup_keys")
        if not isinstance(dedup_keys, list) or not dedup_keys or len(dedup_keys) > 4:
            raise OperationsAlertCatalogError(f"alert {alert_id} has invalid dedup_keys")
        if any(not isinstance(key, str) or key not in SAFE_DEDUP_KEYS for key in dedup_keys):
            raise OperationsAlertCatalogError(f"alert {alert_id} has invalid dedup_keys")
        if len(set(dedup_keys)) != len(dedup_keys):
            raise OperationsAlertCatalogError(f"alert {alert_id} has invalid dedup_keys")

        if alert.get("recovery_notification") is not True:
            raise OperationsAlertCatalogError(
                f"alert {alert_id} must emit a recovery notification"
            )

    return payload


def load_catalog(path: Path, *, repo_root: Path | None = None) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = load_json_authority(handle)
    except OperationsAlertCatalogError:
        raise
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise OperationsAlertCatalogError("catalog cannot be read as strict JSON") from exc
    return validate_catalog(payload, repo_root=repo_root)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the IRLight operations alert catalog")
    parser.add_argument(
        "catalog",
        nargs="?",
        default="config/operations-alert-catalog.json",
        type=Path,
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = parser.parse_args()

    catalog = load_catalog(args.catalog, repo_root=args.repo_root)
    print(f"VALID alerts={len(catalog['alerts'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
