#!/usr/bin/env python3
"""Validate and summarize IRLight long-running soak evidence.

The validator intentionally does not invent release thresholds for resource
trends. It verifies that a claimed successful run contains complete, ordered,
finite observations and verified cleanup, then emits deterministic deltas that
can be reviewed or gated by a separately approved policy.

Canonical report inputs are limited to 2 MiB and must be stable regular files.
The loader rejects symlinks and non-regular files, pins the opened inode, and
rechecks file plus pathname identity after the bounded read so validation cannot
block on a FIFO/device or silently accept bytes changed/replaced during reading.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import stat
import sys
import uuid
from pathlib import Path
from typing import Any


class SoakReportError(ValueError):
    """Raised when a soak report is malformed or internally inconsistent."""


TOP_LEVEL_FIELDS = {
    "schema_version",
    "run_id",
    "scenario",
    "target_duration_seconds",
    "outcome",
    "samples",
    "cleanup",
    "notes",
}
SAMPLE_FIELDS = {
    "elapsed_seconds",
    "memory_rss_bytes",
    "cpu_percent",
    "open_fds",
    "processes",
    "zombies",
    "bitrate_bps",
    "av_sync_drift_ms",
    "timestamp_errors",
    "unexpected_reconnects",
}
CLEANUP_FIELDS = {"verified", "details"}
OUTCOMES = {"pass", "fail", "aborted"}
MAX_REPORT_BYTES = 2 * 1024 * 1024


def _reject_constant(value: str) -> None:
    raise SoakReportError(f"non-standard JSON numeric constant: {value}")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise SoakReportError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _open_report_readonly(path: Path) -> Any:
    """Open one stable regular-file report without following a final symlink."""

    try:
        before = os.lstat(path)
    except OSError as exc:
        raise SoakReportError(f"cannot inspect report: {exc}") from exc
    if not stat.S_ISREG(before.st_mode):
        raise SoakReportError("report must be a regular file")
    if before.st_size > MAX_REPORT_BYTES:
        raise SoakReportError(
            f"report exceeds maximum size of {MAX_REPORT_BYTES} bytes"
        )

    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise SoakReportError(f"cannot open report: {exc}") from exc
    try:
        after = os.fstat(fd)
        if not stat.S_ISREG(after.st_mode):
            raise SoakReportError("report must be a regular file")
        if _stat_identity(before) != _stat_identity(after):
            raise SoakReportError("report changed while opening")
        if after.st_size > MAX_REPORT_BYTES:
            raise SoakReportError(
                f"report exceeds maximum size of {MAX_REPORT_BYTES} bytes"
            )
        return os.fdopen(fd, "rb")
    except Exception:
        os.close(fd)
        raise


def _read_report_bytes(path: Path) -> bytes:
    try:
        with _open_report_readonly(path) as handle:
            before_read = os.fstat(handle.fileno())
            raw_bytes = handle.read(MAX_REPORT_BYTES + 1)
            after_read = os.fstat(handle.fileno())
    except OSError as exc:
        raise SoakReportError(f"cannot read report: {exc}") from exc

    if _stat_identity(before_read) != _stat_identity(after_read):
        raise SoakReportError("report changed while reading")
    if len(raw_bytes) > MAX_REPORT_BYTES:
        raise SoakReportError(
            f"report exceeds maximum size of {MAX_REPORT_BYTES} bytes"
        )
    try:
        final_path = os.lstat(path)
    except OSError as exc:
        raise SoakReportError(f"cannot re-inspect report: {exc}") from exc
    if not stat.S_ISREG(final_path.st_mode):
        raise SoakReportError("report changed while reading")
    if _stat_identity(final_path) != _stat_identity(after_read):
        raise SoakReportError("report changed while reading")
    return raw_bytes


def load_report(path: Path) -> dict[str, Any]:
    try:
        raw = _read_report_bytes(path).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SoakReportError(f"cannot read report: {exc}") from exc
    try:
        value = json.loads(
            raw,
            parse_constant=_reject_constant,
            object_pairs_hook=_strict_object,
        )
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
        raise SoakReportError(f"invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise SoakReportError("report root must be an object")
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
        raise SoakReportError(f"{label} fields do not match schema ({'; '.join(details)})")


def _strict_nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SoakReportError(f"{label} must be a non-negative integer")
    return value


def _strict_positive_int(value: Any, label: str) -> int:
    result = _strict_nonnegative_int(value, label)
    if result == 0:
        raise SoakReportError(f"{label} must be greater than zero")
    return result


def _finite_number(value: Any, label: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SoakReportError(f"{label} must be a finite number")
    try:
        normalized = float(value)
    except (OverflowError, TypeError, ValueError):
        raise SoakReportError(f"{label} must be a finite number") from None
    if not math.isfinite(normalized):
        raise SoakReportError(f"{label} must be a finite number")
    if minimum is not None and normalized < minimum:
        raise SoakReportError(f"{label} must be >= {minimum:g}")
    return normalized


def _optional_nonnegative_number(value: Any, label: str) -> float | None:
    if value is None:
        return None
    return _finite_number(value, label, minimum=0.0)


def _optional_finite_number(value: Any, label: str) -> float | None:
    if value is None:
        return None
    return _finite_number(value, label)


def validate_report(report: dict[str, Any]) -> dict[str, Any]:
    _require_exact_fields(report, TOP_LEVEL_FIELDS, "report")

    if (
        isinstance(report["schema_version"], bool)
        or not isinstance(report["schema_version"], int)
        or report["schema_version"] != 1
    ):
        raise SoakReportError("schema_version must be integer 1")
    if not isinstance(report["run_id"], str):
        raise SoakReportError("run_id must be a UUID string")
    try:
        uuid.UUID(report["run_id"])
    except (ValueError, AttributeError) as exc:
        raise SoakReportError("run_id must be a UUID string") from exc
    scenario = report["scenario"]
    if not isinstance(scenario, str) or not scenario.strip() or len(scenario) > 200:
        raise SoakReportError("scenario must be a non-empty string up to 200 characters")
    target = _strict_positive_int(report["target_duration_seconds"], "target_duration_seconds")
    outcome = report["outcome"]
    if not isinstance(outcome, str) or outcome not in OUTCOMES:
        raise SoakReportError("outcome must be one of: pass, fail, aborted")
    notes = report["notes"]
    if not isinstance(notes, str) or len(notes) > 4000:
        raise SoakReportError("notes must be a string up to 4000 characters")

    cleanup = report["cleanup"]
    if not isinstance(cleanup, dict):
        raise SoakReportError("cleanup must be an object")
    _require_exact_fields(cleanup, CLEANUP_FIELDS, "cleanup")
    if not isinstance(cleanup["verified"], bool):
        raise SoakReportError("cleanup.verified must be boolean")
    if (
        not isinstance(cleanup["details"], str)
        or not cleanup["details"].strip()
        or len(cleanup["details"]) > 1000
    ):
        raise SoakReportError("cleanup.details must be a non-empty string up to 1000 characters")

    samples = report["samples"]
    if not isinstance(samples, list) or not samples:
        raise SoakReportError("samples must be a non-empty array")

    normalized: list[dict[str, Any]] = []
    previous_elapsed: float | None = None
    previous_timestamp_errors: int | None = None
    previous_reconnects: int | None = None
    for index, sample in enumerate(samples):
        label = f"samples[{index}]"
        if not isinstance(sample, dict):
            raise SoakReportError(f"{label} must be an object")
        _require_exact_fields(sample, SAMPLE_FIELDS, label)
        elapsed = _finite_number(sample["elapsed_seconds"], f"{label}.elapsed_seconds", minimum=0.0)
        if previous_elapsed is not None and elapsed <= previous_elapsed:
            raise SoakReportError("sample elapsed_seconds values must be strictly increasing")
        previous_elapsed = elapsed
        timestamp_errors = _strict_nonnegative_int(sample["timestamp_errors"], f"{label}.timestamp_errors")
        reconnects = _strict_nonnegative_int(sample["unexpected_reconnects"], f"{label}.unexpected_reconnects")
        if previous_timestamp_errors is not None and timestamp_errors < previous_timestamp_errors:
            raise SoakReportError("timestamp_errors must be a cumulative non-decreasing counter")
        if previous_reconnects is not None and reconnects < previous_reconnects:
            raise SoakReportError("unexpected_reconnects must be a cumulative non-decreasing counter")
        previous_timestamp_errors = timestamp_errors
        previous_reconnects = reconnects
        normalized.append(
            {
                "elapsed_seconds": elapsed,
                "memory_rss_bytes": _strict_nonnegative_int(sample["memory_rss_bytes"], f"{label}.memory_rss_bytes"),
                "cpu_percent": _finite_number(sample["cpu_percent"], f"{label}.cpu_percent", minimum=0.0),
                "open_fds": _strict_nonnegative_int(sample["open_fds"], f"{label}.open_fds"),
                "processes": _strict_nonnegative_int(sample["processes"], f"{label}.processes"),
                "zombies": _strict_nonnegative_int(sample["zombies"], f"{label}.zombies"),
                "bitrate_bps": _optional_nonnegative_number(sample["bitrate_bps"], f"{label}.bitrate_bps"),
                "av_sync_drift_ms": _optional_finite_number(sample["av_sync_drift_ms"], f"{label}.av_sync_drift_ms"),
                "timestamp_errors": timestamp_errors,
                "unexpected_reconnects": reconnects,
            }
        )

    if outcome == "pass":
        if len(normalized) < 2:
            raise SoakReportError("a passing report requires at least two samples")
        if normalized[0]["elapsed_seconds"] != 0.0:
            raise SoakReportError("a passing report must start with elapsed_seconds 0")
        if normalized[0]["timestamp_errors"] != 0:
            raise SoakReportError("a passing report must baseline timestamp_errors at 0")
        if normalized[0]["unexpected_reconnects"] != 0:
            raise SoakReportError("a passing report must baseline unexpected_reconnects at 0")
        if normalized[-1]["elapsed_seconds"] < target:
            raise SoakReportError("a passing report must cover target_duration_seconds")
        if not cleanup["verified"]:
            raise SoakReportError("a passing report requires verified cleanup")

    first = normalized[0]
    last = normalized[-1]
    bitrate_values = [sample["bitrate_bps"] for sample in normalized if sample["bitrate_bps"] is not None]
    av_values = [abs(sample["av_sync_drift_ms"]) for sample in normalized if sample["av_sync_drift_ms"] is not None]
    summary = {
        "schema_version": 1,
        "run_id": report["run_id"],
        "scenario": scenario,
        "outcome": outcome,
        "target_duration_seconds": target,
        "observed_duration_seconds": last["elapsed_seconds"],
        "sample_count": len(normalized),
        "memory_rss_start_bytes": first["memory_rss_bytes"],
        "memory_rss_end_bytes": last["memory_rss_bytes"],
        "memory_rss_delta_bytes": last["memory_rss_bytes"] - first["memory_rss_bytes"],
        "memory_rss_peak_bytes": max(sample["memory_rss_bytes"] for sample in normalized),
        "cpu_peak_percent": max(sample["cpu_percent"] for sample in normalized),
        "open_fds_start": first["open_fds"],
        "open_fds_end": last["open_fds"],
        "open_fds_delta": last["open_fds"] - first["open_fds"],
        "processes_start": first["processes"],
        "processes_end": last["processes"],
        "processes_delta": last["processes"] - first["processes"],
        "zombies_peak": max(sample["zombies"] for sample in normalized),
        "bitrate_min_bps": min(bitrate_values) if bitrate_values else None,
        "bitrate_max_bps": max(bitrate_values) if bitrate_values else None,
        "av_sync_abs_peak_ms": max(av_values) if av_values else None,
        "timestamp_errors": last["timestamp_errors"],
        "unexpected_reconnects": last["unexpected_reconnects"],
        "cleanup_verified": cleanup["verified"],
    }
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="path to the soak report JSON")
    parser.add_argument("--json", action="store_true", help="print the deterministic summary as JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        summary = validate_report(load_report(args.report))
    except SoakReportError as exc:
        print(f"soak report invalid: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, allow_nan=False))
    else:
        print(
            "soak report valid: "
            f"outcome={summary['outcome']} "
            f"samples={summary['sample_count']} "
            f"observed={summary['observed_duration_seconds']:g}s "
            f"cleanup_verified={str(summary['cleanup_verified']).lower()}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
