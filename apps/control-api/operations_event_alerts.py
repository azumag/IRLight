"""Read-only dry-run evaluator for event-triggered operations alerts.

The alert catalog intentionally separates detection contracts from notification
routing. This module consumes structured JSONL records, matches only explicit
``event`` triggers from that catalog, and emits aggregate alert-ID counts. It
never sends notifications, evaluates threshold alerts, or echoes source values.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Iterable, Mapping
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
    object_without_duplicates,
    reject_nonfinite,
    strict_json_loads,
    utf8_size_violation,
)


_REQUIRED_FIELDS = ("timestamp", "level", "service", "event_type")
_MAX_RECORD_BYTES = 256 * 1024
_MAX_NESTING_DEPTH = 64

# Compatibility aliases keep existing focused tests/callers stable while the
# implementation is shared with other read-only operations JSONL evaluators.
_DuplicateKeyError = DuplicateKeyError
_NonFiniteNumberError = NonFiniteNumberError
_OversizedRecord = OversizedRecord
_InvalidUtf8Record = InvalidUtf8Record
_OVERSIZED_RECORD = OVERSIZED_RECORD
_INVALID_UTF8_RECORD = INVALID_UTF8_RECORD
_reject_nonfinite = reject_nonfinite
_object_without_duplicates = object_without_duplicates


class OperationsEventAlertError(ValueError):
    """Raised when event-alert evaluation cannot be trusted."""


def _build_event_alert_index(catalog: dict[str, Any]) -> dict[str, str]:
    """Return event_type -> alert_id and fail closed on ambiguous routing."""
    alerts = catalog.get("alerts")
    if not isinstance(alerts, list):
        raise OperationsEventAlertError("catalog alerts are unavailable")

    index: dict[str, str] = {}
    for alert in alerts:
        if not isinstance(alert, dict):
            raise OperationsEventAlertError("catalog alert is invalid")
        trigger = alert.get("trigger")
        if not isinstance(trigger, dict) or trigger.get("mode") != "event":
            continue
        event_type = trigger.get("event_type")
        alert_id = alert.get("id")
        if not isinstance(event_type, str) or not isinstance(alert_id, str):
            raise OperationsEventAlertError("event alert mapping is invalid")
        if event_type in index:
            raise OperationsEventAlertError("event_type maps to multiple alerts")
        index[event_type] = alert_id
    return index


def _iter_bounded_lines(stream: TextIO) -> Iterable[str | OversizedRecord]:
    return iter_bounded_lines(stream, max_record_bytes=_MAX_RECORD_BYTES)


def _iter_bounded_byte_lines(
    stream: BinaryIO,
) -> Iterable[str | OversizedRecord | InvalidUtf8Record]:
    return iter_bounded_byte_lines(stream, max_record_bytes=_MAX_RECORD_BYTES)


def _has_excessive_nesting(value: Any) -> bool:
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > _MAX_NESTING_DEPTH:
            return True
        if isinstance(current, Mapping):
            stack.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, list):
            stack.extend((child, depth + 1) for child in current)
    return False


def _valid_required_fields(record: Any, reasons: Counter[str]) -> bool:
    if not isinstance(record, dict):
        reasons["INVALID_RECORD"] += 1
        return False

    if any(field not in record for field in _REQUIRED_FIELDS):
        reasons["MISSING_REQUIRED_FIELD"] += 1
        return False

    if any(
        not isinstance(record[field], str) or not record[field].strip()
        for field in _REQUIRED_FIELDS
    ):
        reasons["INVALID_REQUIRED_FIELD"] += 1
        return False
    return True


def evaluate_lines(
    lines: Iterable[str | OversizedRecord | InvalidUtf8Record],
    catalog: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate JSONL records without returning source content or identifiers.

    If any record is structurally invalid, the whole batch is ``INVALID`` and
    matched/unmatched output is suppressed. This prevents a partial batch from
    being mistaken for a complete detector result.
    """
    event_index = _build_event_alert_index(catalog)
    matches: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    records = 0
    unmatched_records = 0
    invalid = False

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
        except RecursionError:
            reasons["NESTING_TOO_DEEP"] += 1
            invalid = True
            continue
        except (json.JSONDecodeError, UnicodeError, TypeError, ValueError):
            reasons["INVALID_JSON"] += 1
            invalid = True
            continue

        if _has_excessive_nesting(value):
            reasons["NESTING_TOO_DEEP"] += 1
            invalid = True
            continue
        if not _valid_required_fields(value, reasons):
            invalid = True
            continue

        alert_id = event_index.get(value["event_type"])
        if alert_id is None:
            unmatched_records += 1
        else:
            matches[alert_id] += 1

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
        "matched_alerts": dict(sorted(matches.items())),
        "unmatched_records": unmatched_records,
        "violations": {},
    }


def _exit_code(status: str) -> int:
    return {"NO_MATCHES": 0, "MATCHED": 0, "INVALID": 3, "INVALID_CATALOG": 4}[status]


def main(argv: list[str] | None = None, *, stdin: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Dry-run event-triggered IRLight operations alerts from structured JSONL."
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
        _build_event_alert_index(catalog)
    except (OperationsAlertCatalogError, OperationsEventAlertError):
        result = {
            "status": "INVALID_CATALOG",
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
    result = evaluate_lines(lines, catalog)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return _exit_code(result["status"])


if __name__ == "__main__":
    raise SystemExit(main())
