#!/usr/bin/env python3
"""Generate or validate secret-minimal reports for deterministic network-fault cases.

Reports deliberately contain no destination URL, credential, namespace, command argv, or
free-form notes. A generated template starts as ``NOT_RUN`` and cannot be mistaken for
compatibility evidence until an operator records a real result and passes validation.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any, Sequence

MAX_REPORT_BYTES = 32 * 1024
SCHEMA_VERSION = 1
REPORT_RESULTS = {"NOT_RUN", "PASS", "FAIL", "BLOCKED"}
CHECK_RESULTS = {"NOT_RUN", "PASS", "FAIL", "BLOCKED"}
CHECK_NAMES = (
    "fault_applied",
    "workload_active",
    "expected_continuity",
    "cleanup_confirmed",
)
REPORT_FIELDS = {
    "schema_version",
    "case_id",
    "protocol",
    "fault",
    "tested_at",
    "result",
    "checks",
    "metrics",
}
FAULT_FIELDS = {"loss_percent", "latency_ms", "disconnect", "duration_seconds"}
METRIC_FIELDS = {"recovery_seconds", "blackout_seconds", "unexpected_reconnects"}
CHECK_FIELDS = {"name", "result"}


class FaultReportError(ValueError):
    """Raised when a network-fault report is malformed or cannot be tied to a case."""


def _load_matrix() -> ModuleType:
    path = Path(__file__).resolve().with_name("network-fault-matrix.py")
    spec = importlib.util.spec_from_file_location("irlight_network_fault_report_matrix", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("network fault matrix module cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MATRIX = _load_matrix()


def _canonical_case(case_id: str) -> dict[str, Any]:
    """Resolve a stable case ID without executing or exposing namespace-local argv."""

    if not isinstance(case_id, str) or not case_id.strip():
        raise FaultReportError("case_id must be a non-empty string")

    matches: list[dict[str, Any]] = []
    for duration in MATRIX.INJECTOR.DURATION_SECONDS_CHOICES:
        matrix = MATRIX.build_matrix(
            namespace="irlight-report",
            interface="eth0",
            protocols=MATRIX.PROTOCOL_CHOICES,
            profile_duration_seconds=duration,
        )
        for case in matrix["cases"]:
            if case["id"] == case_id:
                matches.append(case)

    if not matches:
        raise FaultReportError("case_id is not in the deterministic network fault matrix")

    first = matches[0]
    canonical = {
        "id": first["id"],
        "protocol": first["protocol"],
        "fault": first["fault"],
    }
    for duplicate in matches[1:]:
        if (
            duplicate["protocol"] != canonical["protocol"]
            or duplicate["fault"] != canonical["fault"]
        ):
            raise FaultReportError("case_id resolves to inconsistent matrix definitions")
    return canonical


def build_template(case_id: str) -> dict[str, Any]:
    case = _canonical_case(case_id)
    return {
        "schema_version": SCHEMA_VERSION,
        "case_id": case["id"],
        "protocol": case["protocol"],
        "fault": case["fault"],
        "tested_at": None,
        "result": "NOT_RUN",
        "checks": [
            {"name": name, "result": "NOT_RUN"}
            for name in CHECK_NAMES
        ],
        "metrics": {
            "recovery_seconds": None,
            "blackout_seconds": None,
            "unexpected_reconnects": None,
        },
    }


def _reject_duplicate_object_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise FaultReportError("duplicate object key is not allowed")
        result[key] = value
    return result


def _reject_nonstandard_constant(value: str) -> None:
    raise FaultReportError(f"non-standard JSON constant is not allowed: {value}")


def load_report(path: Path) -> dict[str, Any]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise FaultReportError("report cannot be read") from exc
    if size > MAX_REPORT_BYTES:
        raise FaultReportError(f"report exceeds {MAX_REPORT_BYTES}-byte limit")

    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_object_keys,
            parse_constant=_reject_nonstandard_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, FaultReportError) as exc:
        if isinstance(exc, FaultReportError):
            raise
        raise FaultReportError("report is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise FaultReportError("report root must be an object")
    return value


def _timestamp_is_aware(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _finite_nonnegative_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def validate_report(report: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    unknown = sorted(str(key) for key in report.keys() if key not in REPORT_FIELDS)
    missing = sorted(REPORT_FIELDS - report.keys())
    if unknown:
        errors.append("unknown report fields: " + ", ".join(unknown))
    if missing:
        errors.append("missing report fields: " + ", ".join(missing))

    schema_version = report.get("schema_version")
    if isinstance(schema_version, bool) or schema_version != SCHEMA_VERSION:
        errors.append(f"schema_version must be integer {SCHEMA_VERSION}")

    case: dict[str, Any] | None = None
    try:
        case = _canonical_case(report.get("case_id"))
    except FaultReportError as exc:
        errors.append(str(exc))

    if case is not None:
        if report.get("protocol") != case["protocol"]:
            errors.append("protocol must match the deterministic matrix case")
        if report.get("fault") != case["fault"]:
            errors.append("fault must exactly match the deterministic matrix case")

    fault = report.get("fault")
    if isinstance(fault, dict):
        if set(fault.keys()) != FAULT_FIELDS:
            errors.append("fault must contain exactly the canonical fault fields")
    else:
        errors.append("fault must be an object")

    result = report.get("result")
    if not isinstance(result, str) or result not in REPORT_RESULTS:
        errors.append(f"result must be one of {sorted(REPORT_RESULTS)}")
        result = None

    tested_at = report.get("tested_at")
    if result == "NOT_RUN":
        if tested_at is not None:
            errors.append("NOT_RUN reports must keep tested_at=null")
    elif result is not None and not _timestamp_is_aware(tested_at):
        errors.append("executed reports require a timezone-aware ISO 8601 tested_at")

    check_results: dict[str, str] = {}
    checks = report.get("checks")
    if not isinstance(checks, list) or len(checks) != len(CHECK_NAMES):
        errors.append("checks must contain exactly the required four checks")
    else:
        for index, check in enumerate(checks):
            prefix = f"checks[{index}]"
            if not isinstance(check, dict):
                errors.append(f"{prefix} must be an object")
                continue
            if set(check.keys()) != CHECK_FIELDS:
                errors.append(f"{prefix} must contain exactly name and result")
                continue
            name = check.get("name")
            check_result = check.get("result")
            if name not in CHECK_NAMES:
                errors.append(f"{prefix}.name is not a required check")
                continue
            if name in check_results:
                errors.append(f"duplicate check name: {name}")
                continue
            if not isinstance(check_result, str) or check_result not in CHECK_RESULTS:
                errors.append(f"{prefix}.result must be one of {sorted(CHECK_RESULTS)}")
                continue
            check_results[name] = check_result
        if set(check_results) != set(CHECK_NAMES):
            errors.append("checks must contain each required check exactly once")

    if result == "NOT_RUN" and check_results:
        if any(value != "NOT_RUN" for value in check_results.values()):
            errors.append("NOT_RUN reports must keep every check at NOT_RUN")
    if result == "PASS" and check_results:
        if any(value != "PASS" for value in check_results.values()):
            errors.append("PASS reports require every check to PASS")
    if result in {"FAIL", "BLOCKED"} and check_results:
        if all(value == "PASS" for value in check_results.values()):
            errors.append(f"{result} reports must identify at least one non-PASS check")
        if any(value == "NOT_RUN" for value in check_results.values()) and result == "FAIL":
            errors.append("FAIL reports must record an outcome for every check")

    metrics = report.get("metrics")
    if not isinstance(metrics, dict):
        errors.append("metrics must be an object")
    else:
        if set(metrics.keys()) != METRIC_FIELDS:
            errors.append("metrics must contain exactly the canonical metric fields")
        for name in ("recovery_seconds", "blackout_seconds"):
            value = metrics.get(name)
            if value is not None and not _finite_nonnegative_number(value):
                errors.append(f"metrics.{name} must be null or a finite non-negative number")
        reconnects = metrics.get("unexpected_reconnects")
        if reconnects is not None and (
            isinstance(reconnects, bool) or not isinstance(reconnects, int) or reconnects < 0
        ):
            errors.append("metrics.unexpected_reconnects must be null or a non-negative integer")
        if result == "NOT_RUN" and any(value is not None for value in metrics.values()):
            errors.append("NOT_RUN reports must keep all metrics null")

    return errors


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    template = subparsers.add_parser("template", help="emit a NOT_RUN report template")
    template.add_argument("--case", required=True, dest="case_id")
    template.add_argument("--pretty", action="store_true")

    validate = subparsers.add_parser("validate", help="validate one completed report")
    validate.add_argument("report", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "template":
            report = build_template(args.case_id)
            print(
                json.dumps(
                    report,
                    ensure_ascii=False,
                    indent=2 if args.pretty else None,
                    sort_keys=True,
                )
            )
            return 0

        report = load_report(args.report)
        errors = validate_report(report)
        if errors:
            for error in errors:
                print(f"network-fault-report: {error}", file=sys.stderr)
            return 1
        print(f"network-fault-report valid: {report['case_id']} ({report['result']})")
        return 0
    except (FaultReportError, MATRIX.MatrixError, MATRIX.INJECTOR.FaultPlanError) as exc:
        print(f"network-fault-report: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
