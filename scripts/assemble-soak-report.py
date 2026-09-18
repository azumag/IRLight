#!/usr/bin/env python3
"""Assemble strict IRLight soak samples into a schema-v1 evidence report."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any


MAX_SAMPLES_JSONL_BYTES = 32 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024


class SoakAssemblyError(ValueError):
    """Raised when sample evidence cannot be safely assembled."""


def _reject_constant(value: str) -> None:
    raise SoakAssemblyError(f"non-standard JSON numeric constant: {value}")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise SoakAssemblyError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _read_samples_bytes(path: Path) -> bytes:
    """Read one stable bounded regular file without following a final symlink."""

    try:
        before = os.lstat(path)
    except OSError as exc:
        raise SoakAssemblyError("cannot inspect samples JSONL") from exc
    if not stat.S_ISREG(before.st_mode):
        raise SoakAssemblyError("samples JSONL must be a regular file")
    if before.st_size > MAX_SAMPLES_JSONL_BYTES:
        raise SoakAssemblyError(
            f"samples JSONL exceeds {MAX_SAMPLES_JSONL_BYTES}-byte limit"
        )

    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise SoakAssemblyError("cannot open samples JSONL") from exc

    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise SoakAssemblyError("samples JSONL must be a regular file")
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise SoakAssemblyError("samples JSONL changed while opening")
        if opened.st_size > MAX_SAMPLES_JSONL_BYTES:
            raise SoakAssemblyError(
                f"samples JSONL exceeds {MAX_SAMPLES_JSONL_BYTES}-byte limit"
            )

        before_read = os.fstat(fd)
        raw = bytearray()
        while len(raw) <= MAX_SAMPLES_JSONL_BYTES:
            remaining = MAX_SAMPLES_JSONL_BYTES + 1 - len(raw)
            try:
                chunk = os.read(fd, min(_READ_CHUNK_BYTES, remaining))
            except OSError as exc:
                raise SoakAssemblyError("cannot read samples JSONL") from exc
            if not chunk:
                break
            raw.extend(chunk)

        after_read = os.fstat(fd)
        if _file_identity(before_read) != _file_identity(after_read):
            raise SoakAssemblyError("samples JSONL changed while reading")
        try:
            current = os.lstat(path)
        except OSError as exc:
            raise SoakAssemblyError("samples JSONL changed while reading") from exc
        if not stat.S_ISREG(current.st_mode) or _file_identity(current) != _file_identity(
            after_read
        ):
            raise SoakAssemblyError("samples JSONL changed while reading")
        if len(raw) > MAX_SAMPLES_JSONL_BYTES:
            raise SoakAssemblyError(
                f"samples JSONL exceeds {MAX_SAMPLES_JSONL_BYTES}-byte limit"
            )
        if len(raw) != after_read.st_size:
            raise SoakAssemblyError("samples JSONL changed while reading")
        return bytes(raw)
    finally:
        os.close(fd)


def load_samples_jsonl(path: Path) -> list[dict[str, Any]]:
    raw_bytes = _read_samples_bytes(path)
    try:
        raw = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SoakAssemblyError("cannot read samples: invalid UTF-8") from exc

    lines = raw.splitlines()
    if not lines:
        raise SoakAssemblyError("samples JSONL must contain at least one sample")

    samples: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            raise SoakAssemblyError(f"samples JSONL line {line_number} must not be blank")
        try:
            value = json.loads(
                line,
                parse_constant=_reject_constant,
                object_pairs_hook=_strict_object,
            )
        except json.JSONDecodeError as exc:
            raise SoakAssemblyError(
                f"invalid JSON on samples JSONL line {line_number}: {exc}"
            ) from exc
        except RecursionError as exc:
            raise SoakAssemblyError(
                f"samples JSONL line {line_number} exceeds JSON nesting limit"
            ) from exc
        if not isinstance(value, dict):
            raise SoakAssemblyError(
                f"samples JSONL line {line_number} must contain a JSON object"
            )
        samples.append(value)
    return samples


def _load_validator() -> Any:
    path = Path(__file__).with_name("validate-soak-report.py")
    spec = importlib.util.spec_from_file_location("irlight_validate_soak_report", path)
    if spec is None or spec.loader is None:
        raise SoakAssemblyError("cannot load soak report validator")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except (OSError, ImportError) as exc:
        raise SoakAssemblyError(f"cannot load soak report validator: {exc}") from exc
    return module


def assemble_report(
    *,
    samples: list[dict[str, Any]],
    run_id: str,
    scenario: str,
    target_duration_seconds: int,
    outcome: str,
    cleanup_verified: bool,
    cleanup_details: str,
    notes: str,
) -> dict[str, Any]:
    report = {
        "schema_version": 1,
        "run_id": run_id,
        "scenario": scenario,
        "target_duration_seconds": target_duration_seconds,
        "outcome": outcome,
        "samples": samples,
        "cleanup": {
            "verified": cleanup_verified,
            "details": cleanup_details,
        },
        "notes": notes,
    }
    validator = _load_validator()
    try:
        validator.validate_report(report)
    except (ValueError, TypeError, OverflowError) as exc:
        raise SoakAssemblyError(f"assembled report is invalid: {exc}") from exc
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples-jsonl", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--target-duration-seconds", type=int, required=True)
    parser.add_argument("--outcome", choices=("pass", "fail", "aborted"), required=True)
    parser.add_argument("--cleanup-verified", action="store_true")
    parser.add_argument("--cleanup-details", required=True)
    parser.add_argument("--notes", default="")
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.output is not None:
            try:
                if args.output.resolve() == args.samples_jsonl.resolve():
                    raise SoakAssemblyError("output must not overwrite the raw samples JSONL")
            except OSError as exc:
                raise SoakAssemblyError(f"cannot resolve paths: {exc}") from exc

        samples = load_samples_jsonl(args.samples_jsonl)
        report = assemble_report(
            samples=samples,
            run_id=args.run_id,
            scenario=args.scenario,
            target_duration_seconds=args.target_duration_seconds,
            outcome=args.outcome,
            cleanup_verified=args.cleanup_verified,
            cleanup_details=args.cleanup_details,
            notes=args.notes,
        )
        rendered = json.dumps(
            report,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
            separators=(",", ":"),
        ) + "\n"
        if args.output is None:
            sys.stdout.write(rendered)
        else:
            with args.output.open("x", encoding="utf-8") as handle:
                handle.write(rendered)
    except (SoakAssemblyError, OSError, UnicodeEncodeError) as exc:
        print(f"soak report assembly failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
