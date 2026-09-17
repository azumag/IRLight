#!/usr/bin/env python3
"""Validate IRLight Media Node capacity load-test evidence and derive a safe cap.

This validator deliberately does not decide whether a CPU, memory, or network
measurement is acceptable. The load harness/operator must classify each trial
as pass/fail under an approved scenario policy. The validator makes the
evidence reproducible and fail-closed, requires a measured pass/fail boundary,
and applies an explicit safety margin to the highest passing concurrency.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import uuid
from pathlib import Path
from typing import Any


class CapacityReportError(ValueError):
    """Raised when capacity evidence is malformed or internally inconsistent."""


TOP_LEVEL_FIELDS = {
    "schema_version",
    "run_id",
    "node_profile",
    "software_revision",
    "scenario",
    "safety_margin_percent",
    "trials",
    "notes",
}
TRIAL_FIELDS = {
    "concurrent_sessions",
    "duration_seconds",
    "outcome",
    "cpu_peak_percent",
    "memory_rss_peak_bytes",
    "egress_peak_bps",
    "failed_sessions",
    "unexpected_reconnects",
}
OUTCOMES = {"pass", "fail"}
REVISION_RE = re.compile(r"^[0-9a-f]{40}$")


def _reject_constant(value: str) -> None:
    raise CapacityReportError(f"non-standard JSON numeric constant: {value}")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CapacityReportError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_report(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CapacityReportError(f"cannot read report: {exc}") from exc
    try:
        value = json.loads(raw, parse_constant=_reject_constant, object_pairs_hook=_strict_object)
    except json.JSONDecodeError as exc:
        raise CapacityReportError(f"invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise CapacityReportError("report root must be an object")
    return value


def _require_exact_fields(value: dict[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        details: list[str] = []
        if missing:
            details.append(f"missing={','.join(missing)}")
        if extra:
            details.append(f"extra={','.join(extra)}")
        raise CapacityReportError(f"{label} fields do not match schema ({'; '.join(details)})")


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CapacityReportError(f"{label} must be a positive integer")
    return value


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CapacityReportError(f"{label} must be a non-negative integer")
    return value


def _finite_number(value: Any, label: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CapacityReportError(f"{label} must be a finite number")
    try:
        normalized = float(value)
    except (OverflowError, TypeError, ValueError):
        raise CapacityReportError(f"{label} must be a finite number") from None
    if not math.isfinite(normalized):
        raise CapacityReportError(f"{label} must be a finite number")
    if minimum is not None and normalized < minimum:
        raise CapacityReportError(f"{label} must be >= {minimum:g}")
    return normalized


def _bounded_text(value: Any, label: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise CapacityReportError(f"{label} must be a non-empty string up to {maximum} characters")
    return value


def normalize_trials(trials: Any, *, minimum_count: int = 1) -> list[dict[str, Any]]:
    """Validate trial records and their monotonic ordering without deriving policy."""

    if isinstance(minimum_count, bool) or not isinstance(minimum_count, int) or minimum_count < 1:
        raise ValueError("minimum_count must be a positive integer")
    if not isinstance(trials, list) or len(trials) < minimum_count:
        noun = "load level" if minimum_count == 1 else "load levels"
        raise CapacityReportError(
            f"trials must contain at least {minimum_count} {noun}"
        )

    normalized: list[dict[str, Any]] = []
    previous_sessions = 0
    failure_seen = False
    for index, trial in enumerate(trials):
        label = f"trials[{index}]"
        if not isinstance(trial, dict):
            raise CapacityReportError(f"{label} must be an object")
        _require_exact_fields(trial, TRIAL_FIELDS, label)
        sessions = _positive_int(trial["concurrent_sessions"], f"{label}.concurrent_sessions")
        if sessions <= previous_sessions:
            raise CapacityReportError("trial concurrent_sessions values must be strictly increasing")
        previous_sessions = sessions
        outcome = trial["outcome"]
        if not isinstance(outcome, str) or outcome not in OUTCOMES:
            raise CapacityReportError(f"{label}.outcome must be one of: pass, fail")
        if failure_seen and outcome == "pass":
            raise CapacityReportError("a passing trial cannot appear above a failed load level")
        failure_seen = failure_seen or outcome == "fail"

        failed_sessions = _nonnegative_int(trial["failed_sessions"], f"{label}.failed_sessions")
        reconnects = _nonnegative_int(
            trial["unexpected_reconnects"], f"{label}.unexpected_reconnects"
        )
        if failed_sessions > sessions:
            raise CapacityReportError(f"{label}.failed_sessions cannot exceed concurrent_sessions")
        if outcome == "pass" and (failed_sessions != 0 or reconnects != 0):
            raise CapacityReportError(
                f"{label} cannot claim pass with failed sessions or unexpected reconnects"
            )

        normalized.append(
            {
                "concurrent_sessions": sessions,
                "duration_seconds": _finite_number(
                    trial["duration_seconds"], f"{label}.duration_seconds", minimum=0.001
                ),
                "outcome": outcome,
                "cpu_peak_percent": _finite_number(
                    trial["cpu_peak_percent"], f"{label}.cpu_peak_percent", minimum=0.0
                ),
                "memory_rss_peak_bytes": _nonnegative_int(
                    trial["memory_rss_peak_bytes"], f"{label}.memory_rss_peak_bytes"
                ),
                "egress_peak_bps": _finite_number(
                    trial["egress_peak_bps"], f"{label}.egress_peak_bps", minimum=0.0
                ),
                "failed_sessions": failed_sessions,
                "unexpected_reconnects": reconnects,
            }
        )
    return normalized


def validate_report(report: dict[str, Any]) -> dict[str, Any]:
    _require_exact_fields(report, TOP_LEVEL_FIELDS, "report")

    if (
        isinstance(report["schema_version"], bool)
        or not isinstance(report["schema_version"], int)
        or report["schema_version"] != 1
    ):
        raise CapacityReportError("schema_version must be integer 1")
    if not isinstance(report["run_id"], str):
        raise CapacityReportError("run_id must be a UUID string")
    try:
        uuid.UUID(report["run_id"])
    except (ValueError, AttributeError) as exc:
        raise CapacityReportError("run_id must be a UUID string") from exc

    node_profile = _bounded_text(report["node_profile"], "node_profile", maximum=300)
    revision = report["software_revision"]
    if not isinstance(revision, str) or not REVISION_RE.fullmatch(revision):
        raise CapacityReportError("software_revision must be a lowercase 40-character Git commit SHA")
    scenario = _bounded_text(report["scenario"], "scenario", maximum=300)
    notes = report["notes"]
    if not isinstance(notes, str) or len(notes) > 4000:
        raise CapacityReportError("notes must be a string up to 4000 characters")

    margin = report["safety_margin_percent"]
    if isinstance(margin, bool) or not isinstance(margin, int) or not 1 <= margin <= 90:
        raise CapacityReportError("safety_margin_percent must be an integer from 1 through 90")

    normalized = normalize_trials(report["trials"], minimum_count=2)

    passing = [trial for trial in normalized if trial["outcome"] == "pass"]
    failing = [trial for trial in normalized if trial["outcome"] == "fail"]
    if not passing:
        raise CapacityReportError("report must include at least one passing load level")
    if not failing:
        raise CapacityReportError("report must include a failing load level above the passing boundary")

    highest_pass = passing[-1]["concurrent_sessions"]
    first_fail = failing[0]["concurrent_sessions"]
    if first_fail <= highest_pass:
        raise CapacityReportError("first failing load level must be above the highest passing level")

    recommended = highest_pass * (100 - margin) // 100
    if recommended < 1:
        raise CapacityReportError(
            "evidence does not support a positive max_sessions after applying the safety margin"
        )

    return {
        "schema_version": 1,
        "run_id": report["run_id"],
        "node_profile": node_profile,
        "software_revision": revision,
        "scenario": scenario,
        "tested_load_levels": [trial["concurrent_sessions"] for trial in normalized],
        "highest_passing_sessions": highest_pass,
        "first_failing_sessions": first_fail,
        "safety_margin_percent": margin,
        "recommended_max_sessions": recommended,
        "highest_pass_duration_seconds": passing[-1]["duration_seconds"],
        "highest_pass_cpu_peak_percent": passing[-1]["cpu_peak_percent"],
        "highest_pass_memory_rss_peak_bytes": passing[-1]["memory_rss_peak_bytes"],
        "highest_pass_egress_peak_bps": passing[-1]["egress_peak_bps"],
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="path to the Node capacity load-test report JSON")
    parser.add_argument("--json", action="store_true", help="print the deterministic capacity summary as JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        summary = validate_report(load_report(args.report))
    except CapacityReportError as exc:
        print(f"node capacity report invalid: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, allow_nan=False))
    else:
        print(
            "node capacity report valid: "
            f"highest_pass={summary['highest_passing_sessions']} "
            f"first_fail={summary['first_failing_sessions']} "
            f"margin={summary['safety_margin_percent']}% "
            f"recommended_max_sessions={summary['recommended_max_sessions']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
