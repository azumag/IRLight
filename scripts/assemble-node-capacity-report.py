#!/usr/bin/env python3
"""Assemble strict IRLight Node-capacity trial JSONL into a schema-v1 report."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


class CapacityAssemblyError(ValueError):
    """Raised when raw capacity evidence cannot be safely assembled."""


def _reject_constant(value: str) -> None:
    raise CapacityAssemblyError(f"non-standard JSON numeric constant: {value}")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CapacityAssemblyError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_trials_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CapacityAssemblyError(f"cannot read trials: {exc}") from exc

    lines = raw.splitlines()
    if not lines:
        raise CapacityAssemblyError("trials JSONL must contain at least one trial")

    trials: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            raise CapacityAssemblyError(
                f"trials JSONL line {line_number} must not be blank"
            )
        try:
            value = json.loads(
                line,
                parse_constant=_reject_constant,
                object_pairs_hook=_strict_object,
            )
        except json.JSONDecodeError as exc:
            raise CapacityAssemblyError(
                f"invalid JSON on trials JSONL line {line_number}: {exc}"
            ) from exc
        if not isinstance(value, dict):
            raise CapacityAssemblyError(
                f"trials JSONL line {line_number} must contain a JSON object"
            )
        trials.append(value)
    return trials


def _load_validator() -> Any:
    path = Path(__file__).with_name("validate-node-capacity-report.py")
    spec = importlib.util.spec_from_file_location(
        "irlight_validate_node_capacity_report", path
    )
    if spec is None or spec.loader is None:
        raise CapacityAssemblyError("cannot load Node capacity report validator")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except (OSError, ImportError) as exc:
        raise CapacityAssemblyError(
            f"cannot load Node capacity report validator: {exc}"
        ) from exc
    return module


def assemble_report(
    *,
    trials: list[dict[str, Any]],
    run_id: str,
    node_profile: str,
    software_revision: str,
    scenario: str,
    safety_margin_percent: int,
    notes: str,
) -> dict[str, Any]:
    report = {
        "schema_version": 1,
        "run_id": run_id,
        "node_profile": node_profile,
        "software_revision": software_revision,
        "scenario": scenario,
        "safety_margin_percent": safety_margin_percent,
        "trials": trials,
        "notes": notes,
    }
    validator = _load_validator()
    try:
        validator.validate_report(report)
    except (ValueError, TypeError, OverflowError) as exc:
        raise CapacityAssemblyError(f"assembled report is invalid: {exc}") from exc
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials-jsonl", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--node-profile", required=True)
    parser.add_argument("--software-revision", required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--safety-margin-percent", type=int, required=True)
    parser.add_argument("--notes", default="")
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.output is not None:
            try:
                if args.output.resolve() == args.trials_jsonl.resolve():
                    raise CapacityAssemblyError(
                        "output must not overwrite the raw trials JSONL"
                    )
            except OSError as exc:
                raise CapacityAssemblyError(f"cannot resolve paths: {exc}") from exc

        trials = load_trials_jsonl(args.trials_jsonl)
        report = assemble_report(
            trials=trials,
            run_id=args.run_id,
            node_profile=args.node_profile,
            software_revision=args.software_revision,
            scenario=args.scenario,
            safety_margin_percent=args.safety_margin_percent,
            notes=args.notes,
        )
        rendered = (
            json.dumps(
                report,
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
                separators=(",", ":"),
            )
            + "\n"
        )
        if args.output is None:
            sys.stdout.write(rendered)
        else:
            with args.output.open("x", encoding="utf-8") as handle:
                handle.write(rendered)
    except (CapacityAssemblyError, OSError, UnicodeEncodeError) as exc:
        print(f"node capacity report assembly failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
