#!/usr/bin/env python3
"""Assemble strict IRLight soak samples into a schema-v1 evidence report."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


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


def load_samples_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SoakAssemblyError(f"cannot read samples: {exc}") from exc

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
