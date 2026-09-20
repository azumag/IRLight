#!/usr/bin/env python3
"""Assemble Node-capacity evidence only when it covers one canonical scenario plan.

The lower-level ``assemble-node-capacity-report.py`` intentionally validates the
trial/report schema without knowing which load plan produced the measurements.
This entry point adds that missing provenance gate: the canonical Issue #13 plan
is validated first and the raw trial concurrency ladder must exactly match the
selected scenario before a report can be emitted.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


class PlannedReportError(ValueError):
    """Raised when raw capacity evidence is not complete for its canonical plan."""


def _load_script(filename: str, module_name: str) -> ModuleType:
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise PlannedReportError(f"cannot load {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # pragma: no cover - repository packaging failure
        raise PlannedReportError(f"cannot load {filename}") from exc
    return module


def assemble_planned_report(
    *,
    plan_path: Path,
    trials_path: Path,
    scenario_id: str,
    run_id: str,
    node_profile: str,
    software_revision: str,
    safety_margin_percent: int,
    notes: str,
) -> dict[str, Any]:
    plan_validator = _load_script(
        "validate-node-capacity-load-plan.py",
        "irlight_node_capacity_load_plan_validator_for_planned_report",
    )
    assembler = _load_script(
        "assemble-node-capacity-report.py",
        "irlight_node_capacity_report_assembler_for_planned_report",
    )
    report_validator = _load_script(
        "validate-node-capacity-report.py",
        "irlight_node_capacity_report_validator_for_planned_report",
    )
    coverage_validator = _load_script(
        "validate-node-capacity-plan-coverage.py",
        "irlight_node_capacity_coverage_validator_for_planned_report",
    )

    try:
        plan = plan_validator.load_plan(plan_path)
        plan_validator.validate_plan(plan)
    except plan_validator.PlanValidationError as exc:
        raise PlannedReportError("load plan is not canonical") from exc

    scenario = next(
        (
            item
            for item in plan["scenarios"]
            if isinstance(item, dict) and item.get("id") == scenario_id
        ),
        None,
    )
    if scenario is None:
        raise PlannedReportError("scenario is not present in the canonical load plan")

    try:
        trials = assembler.load_trials_snapshot(trials_path)
        normalized = report_validator.normalize_trials(trials, minimum_count=1)
    except (assembler.CapacityAssemblyError, ValueError, TypeError, OverflowError) as exc:
        raise PlannedReportError("raw trials are invalid") from exc

    measured_levels = [trial["concurrent_sessions"] for trial in normalized]
    planned_levels = list(scenario["session_counts"])
    if measured_levels != planned_levels:
        raise PlannedReportError(
            "raw trials do not cover the complete canonical scenario plan"
        )

    # Coverage validation requires every report to carry a machine-checkable
    # profile+scenario prefix. Derive it from the already-validated plan instead
    # of accepting a second operator-controlled scenario description.
    report_scenario = coverage_validator.expected_report_scenario_prefix(
        plan["profile_label"], scenario_id
    )
    try:
        return assembler.assemble_report(
            trials=normalized,
            run_id=run_id,
            node_profile=node_profile,
            software_revision=software_revision,
            scenario=report_scenario,
            safety_margin_percent=safety_margin_percent,
            notes=notes,
        )
    except assembler.CapacityAssemblyError as exc:
        raise PlannedReportError("assembled report is invalid") from exc


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--trials-jsonl", type=Path, required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--node-profile", required=True)
    parser.add_argument("--software-revision", required=True)
    parser.add_argument("--safety-margin-percent", type=int, required=True)
    parser.add_argument("--notes", default="")
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = assemble_planned_report(
            plan_path=args.plan,
            trials_path=args.trials_jsonl,
            scenario_id=args.scenario,
            run_id=args.run_id,
            node_profile=args.node_profile,
            software_revision=args.software_revision,
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
            assembler = _load_script(
                "assemble-node-capacity-report.py",
                "irlight_node_capacity_report_writer_for_planned_report",
            )
            assembler._write_exclusive_atomic(args.output, rendered)
    except (PlannedReportError, OSError, UnicodeError, ValueError) as exc:
        print(f"planned Node capacity report assembly failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
