"""Read-only dry-run evaluator for the Issue #11 Media Node capacity warning.

The production source of ``max_sessions`` remains the scheduler / Node inventory
validated by load testing.  This command deliberately accepts only aggregate
capacity counts and never queries providers, creates Nodes, changes Session
state, or substitutes entitlement / CPU / memory values for capacity.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any, BinaryIO, TextIO

from operations_alert_catalog import OperationsAlertCatalogError, load_catalog


_ALERT_ID = "MEDIA_NODE_CAPACITY_HIGH"
_SIGNAL = "media_nodes.capacity_ratio"
_THRESHOLD_REF = "issue11.media_node_capacity_80_percent"
_RUNBOOK = "docs/operations/media-node-capacity-high.md"
_REQUIRED_FIELDS = {"max_sessions", "active_sessions", "reserved_sessions"}
_MAX_RECORD_BYTES = 4 * 1024


class OperationsCapacityAlertError(ValueError):
    """Raised when the capacity-alert contract cannot be trusted."""


class _DuplicateKeyError(ValueError):
    pass


class _NonFiniteNumberError(ValueError):
    pass


class _OversizedRecord:
    pass


class _InvalidUtf8Record:
    pass


_OVERSIZED_RECORD = _OversizedRecord()
_INVALID_UTF8_RECORD = _InvalidUtf8Record()


def _reject_nonfinite(value: str) -> None:
    raise _NonFiniteNumberError(value)


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(key)
        result[key] = value
    return result


def _validate_capacity_alert_contract(catalog: dict[str, Any]) -> None:
    alerts = catalog.get("alerts")
    if not isinstance(alerts, list):
        raise OperationsCapacityAlertError("catalog alerts are unavailable")

    matches = [alert for alert in alerts if isinstance(alert, dict) and alert.get("id") == _ALERT_ID]
    if len(matches) != 1:
        raise OperationsCapacityAlertError("capacity alert contract is unavailable")

    alert = matches[0]
    if (
        alert.get("severity") != "warning"
        or alert.get("signal") != _SIGNAL
        or alert.get("trigger")
        != {"mode": "threshold", "threshold_ref": _THRESHOLD_REF}
        or alert.get("runbook") != _RUNBOOK
    ):
        raise OperationsCapacityAlertError("capacity alert contract does not match evaluator")


def _iter_bounded_lines(stream: TextIO) -> Iterable[str | _OversizedRecord]:
    read_limit = _MAX_RECORD_BYTES + 1
    while True:
        line = stream.readline(read_limit)
        if line == "":
            return
        if line.endswith("\n") or len(line) < read_limit:
            yield line
            continue
        while line and not line.endswith("\n"):
            line = stream.readline(read_limit)
        yield _OVERSIZED_RECORD


def _iter_bounded_byte_lines(
    stream: BinaryIO,
) -> Iterable[str | _OversizedRecord | _InvalidUtf8Record]:
    """Bound raw JSONL bytes before decoding so one record cannot grow unbounded."""
    read_limit = _MAX_RECORD_BYTES + 1
    while True:
        line = stream.readline(read_limit)
        if line == b"":
            return
        if line.endswith(b"\n") or len(line) < read_limit:
            if len(line) > _MAX_RECORD_BYTES:
                yield _OVERSIZED_RECORD
                continue
            try:
                yield line.decode("utf-8")
            except UnicodeDecodeError:
                yield _INVALID_UTF8_RECORD
            continue
        while line and not line.endswith(b"\n"):
            line = stream.readline(read_limit)
        yield _OVERSIZED_RECORD


def _capacity_integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _is_above_issue11_threshold(*, maximum: int, active: int, reserved: int) -> bool:
    """Return true only when occupancy is strictly greater than 80 percent.

    Integer cross multiplication avoids floating-point boundary ambiguity:
    ``(active + reserved) / maximum > 0.80`` iff
    ``(active + reserved) * 5 > maximum * 4``.
    """
    return (active + reserved) * 5 > maximum * 4


def evaluate_lines(
    lines: Iterable[str | _OversizedRecord | _InvalidUtf8Record],
    catalog: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate aggregate capacity observations without echoing source values.

    One invalid record invalidates the whole batch and suppresses any partial
    matches.  Inputs intentionally contain no environment, Node, Session, user,
    URL, or credential fields; notification dedup/routing belongs to a later
    collector integration.
    """
    _validate_capacity_alert_contract(catalog)
    matches = 0
    records = 0
    unmatched_records = 0
    invalid = False
    reasons: Counter[str] = Counter()

    for line in lines:
        if line is _OVERSIZED_RECORD:
            records += 1
            reasons["RECORD_TOO_LARGE"] += 1
            invalid = True
            continue
        if line is _INVALID_UTF8_RECORD:
            records += 1
            reasons["INVALID_JSON"] += 1
            invalid = True
            continue

        try:
            record_bytes = len(line.encode("utf-8"))
        except UnicodeEncodeError:
            records += 1
            reasons["INVALID_JSON"] += 1
            invalid = True
            continue
        if record_bytes > _MAX_RECORD_BYTES:
            records += 1
            reasons["RECORD_TOO_LARGE"] += 1
            invalid = True
            continue
        if not line.strip():
            continue

        records += 1
        try:
            value = json.loads(
                line,
                object_pairs_hook=_object_without_duplicates,
                parse_constant=_reject_nonfinite,
            )
        except _DuplicateKeyError:
            reasons["DUPLICATE_JSON_KEY"] += 1
            invalid = True
            continue
        except _NonFiniteNumberError:
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

        maximum = _capacity_integer(value["max_sessions"])
        active = _capacity_integer(value["active_sessions"])
        reserved = _capacity_integer(value["reserved_sessions"])
        if maximum is None or active is None or reserved is None or maximum == 0:
            reasons["INVALID_CAPACITY_VALUE"] += 1
            invalid = True
            continue

        if _is_above_issue11_threshold(maximum=maximum, active=active, reserved=reserved):
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
    return {"NO_MATCHES": 0, "MATCHED": 0, "INVALID": 3, "INVALID_CATALOG": 4}[status]


def main(argv: list[str] | None = None, *, stdin: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Dry-run the Issue #11 Media Node capacity >80% warning from aggregate JSONL."
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
        _validate_capacity_alert_contract(catalog)
    except (OperationsAlertCatalogError, OperationsCapacityAlertError):
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
