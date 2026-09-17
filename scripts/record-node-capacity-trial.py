#!/usr/bin/env python3
"""Append one measured Node-capacity trial to a strict raw JSONL evidence file."""

from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any


class CapacityTrialRecordError(ValueError):
    """Raised when a raw capacity trial cannot be safely recorded."""


def _load_script(filename: str, module_name: str) -> Any:
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise CapacityTrialRecordError(f"cannot load {filename}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except (OSError, ImportError) as exc:
        raise CapacityTrialRecordError(f"cannot load {filename}: {exc}") from exc
    return module


def build_trial(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "concurrent_sessions": args.concurrent_sessions,
        "duration_seconds": args.duration_seconds,
        "outcome": args.outcome,
        "cpu_peak_percent": args.cpu_peak_percent,
        "memory_rss_peak_bytes": args.memory_rss_peak_bytes,
        "egress_peak_bps": args.egress_peak_bps,
        "failed_sessions": args.failed_sessions,
        "unexpected_reconnects": args.unexpected_reconnects,
    }


def _open_lock(path: Path) -> Any:
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise CapacityTrialRecordError(f"cannot open trial lock: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise CapacityTrialRecordError("trial lock must be a regular file")
        return os.fdopen(fd, "r+", encoding="utf-8")
    except Exception:
        os.close(fd)
        raise


def _append_line(path: Path, line: str) -> None:
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise CapacityTrialRecordError(f"cannot open trials JSONL for append: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise CapacityTrialRecordError("trials JSONL must be a regular file")
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            fd = -1
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if fd >= 0:
            os.close(fd)


def append_trial(path: Path, trial: dict[str, Any]) -> dict[str, Any]:
    if path.parent and not path.parent.exists():
        raise CapacityTrialRecordError("trials JSONL parent directory does not exist")

    assembler = _load_script(
        "assemble-node-capacity-report.py",
        "irlight_assemble_node_capacity_report_for_recorder",
    )
    validator = _load_script(
        "validate-node-capacity-report.py",
        "irlight_validate_node_capacity_report_for_recorder",
    )
    try:
        recorded = validator.normalize_trials([trial], minimum_count=1)[0]
    except (ValueError, TypeError, OverflowError) as exc:
        raise CapacityTrialRecordError(f"trial is invalid: {exc}") from exc

    lock_path = path.with_name(f"{path.name}.lock")
    with _open_lock(lock_path) as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)

        if path.is_symlink():
            raise CapacityTrialRecordError("trials JSONL must not be a symbolic link")
        if path.exists() and not path.is_file():
            raise CapacityTrialRecordError("trials JSONL must be a regular file")

        try:
            existing = assembler.load_trials_jsonl(path) if path.exists() else []
            normalized = validator.normalize_trials(existing + [recorded], minimum_count=1)
        except (ValueError, TypeError, OverflowError) as exc:
            raise CapacityTrialRecordError(f"trial sequence is invalid: {exc}") from exc

        recorded = normalized[-1]
        line = (
            json.dumps(
                recorded,
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
                separators=(",", ":"),
            )
            + "\n"
        )
        _append_line(path, line)
        return recorded


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials-jsonl", type=Path, required=True)
    parser.add_argument("--concurrent-sessions", type=int, required=True)
    parser.add_argument("--duration-seconds", type=float, required=True)
    parser.add_argument("--outcome", choices=("pass", "fail"), required=True)
    parser.add_argument("--cpu-peak-percent", type=float, required=True)
    parser.add_argument("--memory-rss-peak-bytes", type=int, required=True)
    parser.add_argument("--egress-peak-bps", type=float, required=True)
    parser.add_argument("--failed-sessions", type=int, required=True)
    parser.add_argument("--unexpected-reconnects", type=int, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        recorded = append_trial(args.trials_jsonl, build_trial(args))
    except (CapacityTrialRecordError, OSError, UnicodeError) as exc:
        print(f"node capacity trial record failed: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            recorded,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
