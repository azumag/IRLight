"""Read-only dry-run evaluator for the Session failure-surge alert.

The repository deliberately does not choose a production failure-rate threshold
or aggregation window. This command accepts already-aggregated failure-rate
observations plus an explicit operator/deployment threshold, validates the
catalog contract, and emits only aggregate alert counts. It never reads or
changes Session authority, starts/stops media processes, calls providers, or
sends notifications.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any, BinaryIO, TextIO

from operations_alert_catalog import OperationsAlertCatalogError, load_catalog
from operations_jsonl_safety import (
    INVALID_UTF8_RECORD,
    OVERSIZED_RECORD,
    DuplicateKeyError,
    InvalidUtf8Record,
    NonFiniteNumberError,
    OversizedRecord,
    iter_bounded_byte_lines,
    iter_bounded_lines,
    strict_json_loads,
    utf8_size_violation,
)


_ALERT_ID = "SESSION_FAILURE_SURGE"
_SIGNAL = "sessions.failed_rate"
_THRESHOLD_REF = "operations.session_failure_surge"
_RUNBOOK = "docs/operations/session-process-crash-loop.md"
_REQUIRED_FIELDS = {"failed_rate"}
_MAX_RECORD_BYTES = 4 * 1024


class OperationsSessionFailureAlertError(ValueError):
    """Raised when the failure-rate alert contract cannot be trusted."""


def _validate_alert_contract(catalog: dict[str, Any]) -> None:
    alerts = catalog.get("alerts")
    if not isinstance(alerts, list):
        raise OperationsSessionFailureAlertError("catalog alerts are unavailable")

    matches = [
        alert
        for alert in alerts
        if isinstance(alert, dict) and alert.get("id") == _ALERT_ID
    ]
    if len(matches) != 1:
        raise OperationsSessionFailureAlertError("failure alert contract is unavailable")

    alert = matches[0]
    if (
        alert.get("severity") != "critical"
        or alert.get("signal") != _SIGNAL
        or alert.get("trigger")
        != {"mode": "threshold", "threshold_ref": _THRESHOLD_REF}
        or alert.get("runbook") != _RUNBOOK
    ):
        raise OperationsSessionFailureAlertError(
            "failure alert contract does not match evaluator"
        )


def _finite_nonnegative_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        converted = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    if not math.isfinite(converted) or converted < 0:
        return None
    return converted


def _validated_threshold(value: Any) -> float:
    threshold = _finite_nonnegative_number(value)
    if threshold is None:
        raise OperationsSessionFailureAlertError(
            "failure threshold must be a finite non-negative number"
        )
    return threshold


def _iter_bounded_lines(stream: TextIO) -> Iterable[str | OversizedRecord]:
    return iter_bounded_lines(stream, max_record_bytes=_MAX_RECORD_BYTES)


def _iter_bounded_byte_lines(
    stream: BinaryIO,
) -> Iterable[str | OversizedRecord | InvalidUtf8Record]:
    return iter_bounded_byte_lines(stream, max_record_bytes=_MAX_RECORD_BYTES)


def evaluate_lines(
    lines: Iterable[str | OversizedRecord | InvalidUtf8Record],
    catalog: dict[str, Any],
    *,
    threshold: float,
) -> dict[str, Any]:
    """Evaluate aggregate failure-rate observations without echoing values.

    The aggregation window and production threshold are intentionally supplied
    outside this module. Any invalid record invalidates the whole batch and
    suppresses partial matches so incomplete data cannot be mistaken for a
    trustworthy alert result.
    """
    _validate_alert_contract(catalog)
    effective_threshold = _validated_threshold(threshold)

    matches = 0
    records = 0
    unmatched_records = 0
    invalid = False
    reasons: Counter[str] = Counter()

    for line in lines:
        if line is OVERSIZED_RECORD:
            records += 1
            reasons["RECORD_TOO_LARGE"] += 1
            invalid = True
            continue
        if line is INVALID_UTF8_RECORD:
            records += 1
            reasons["INVALID_JSON"] += 1
            invalid = True
            continue

        size_violation = utf8_size_violation(line, max_record_bytes=_MAX_RECORD_BYTES)
        if size_violation is not None:
            records += 1
            reasons[size_violation] += 1
            invalid = True
            continue
        if not line.strip():
            continue

        records += 1
        try:
            value = strict_json_loads(line)
        except DuplicateKeyError:
            reasons["DUPLICATE_JSON_KEY"] += 1
            invalid = True
            continue
        except NonFiniteNumberError:
            reasons["NONFINITE_NUMBER"] += 1
            invalid = True
            continue
        except (json.JSONDecodeError, UnicodeError, TypeError, ValueError, RecursionError):
            reasons["INVALID_JSON"] += 1
            invalid = True
            continue

        if not isinstance(value, dict) or set(value) != _REQUIRED_FIELDS:
            reasons["INVALID_RECORD_FIELDS"] += 1
            invalid = True
            continue

        failed_rate = _finite_nonnegative_number(value["failed_rate"])
        if failed_rate is None:
            reasons["INVALID_FAILURE_RATE"] += 1
            invalid = True
            continue

        if failed_rate > effective_threshold:
            matches += 1
        else:
            unmatched_records += 1

    if invalid:
        return {
            "status": "INVALID",
            "records": records,
            "matched_alerts": {},
            "unmatched_records": 0,
            "violations": dict(sorted(reasons.items())),
        }

    return {
        "status": "MATCHED" if matches else "NO_MATCHES",
        "records": records,
        "matched_alerts": {_ALERT_ID: matches} if matches else {},
        "unmatched_records": unmatched_records,
        "violations": {},
    }


def _exit_code(status: str) -> int:
    return {
        "NO_MATCHES": 0,
        "MATCHED": 0,
        "INVALID": 3,
        "INVALID_CATALOG": 4,
        "INVALID_THRESHOLD": 5,
    }[status]


def main(argv: list[str] | None = None, *, stdin: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Dry-run the IRLight Session failure-surge alert from aggregate "
            "failure-rate JSONL."
        )
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=Path("config/operations-alert-catalog.json"),
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--threshold",
        type=float,
        required=True,
        help=(
            "Deployment/operator failure-rate threshold. The repository does not "
            "provide a production default."
        ),
    )
    args = parser.parse_args(argv)

    try:
        catalog = load_catalog(args.catalog, repo_root=args.repo_root)
        _validate_alert_contract(catalog)
    except (OperationsAlertCatalogError, OperationsSessionFailureAlertError):
        result = {
            "status": "INVALID_CATALOG",
            "records": 0,
            "matched_alerts": {},
            "unmatched_records": 0,
            "violations": {},
        }
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return _exit_code(result["status"])

    try:
        threshold = _validated_threshold(args.threshold)
    except OperationsSessionFailureAlertError:
        result = {
            "status": "INVALID_THRESHOLD",
            "records": 0,
            "matched_alerts": {},
            "unmatched_records": 0,
            "violations": {},
        }
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return _exit_code(result["status"])

    source = stdin if stdin is not None else sys.stdin
    raw_source = getattr(source, "buffer", None)
    lines = (
        _iter_bounded_byte_lines(raw_source)
        if raw_source is not None
        else _iter_bounded_lines(source)
    )
    result = evaluate_lines(lines, catalog, threshold=threshold)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return _exit_code(result["status"])


if __name__ == "__main__":
    raise SystemExit(main())
