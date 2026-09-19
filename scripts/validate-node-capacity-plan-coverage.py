#!/usr/bin/env python3
"""Validate that measured Node-capacity reports cover one canonical load plan."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable


class CapacityCoverageError(ValueError):
    """Raised when measured capacity evidence does not cover a canonical plan."""


def _load_script(filename: str, module_name: str) -> ModuleType:
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise CapacityCoverageError(f"cannot load {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # pragma: no cover - repository packaging failure
        raise CapacityCoverageError(f"cannot load {filename}") from exc
    return module


def parse_report_binding(raw: str) -> tuple[str, Path]:
    scenario_id, separator, raw_path = raw.partition("=")
    scenario_id = scenario_id.strip()
    raw_path = raw_path.strip()
    if not separator or not scenario_id or not raw_path:
        raise argparse.ArgumentTypeError("must use SCENARIO_ID=REPORT.json")
    if any(character in scenario_id for character in ("\x00", "\n", "\r")):
        raise argparse.ArgumentTypeError("scenario ID must be one non-empty line")
    return scenario_id, Path(raw_path)


def normalize_report_bindings(
    bindings: Iterable[tuple[str, Path]],
) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for scenario_id, path in bindings:
        if scenario_id in result:
            raise CapacityCoverageError(f"duplicate report binding for scenario: {scenario_id}")
        result[scenario_id] = path
    return result


def _validate_report(
    report_validator: ModuleType,
    path: Path,
) -> dict[str, Any]:
    try:
        return report_validator.validate_report(report_validator.load_report(path))
    except report_validator.CapacityReportError as exc:
        raise CapacityCoverageError("capacity report is invalid") from exc


def validate_coverage(
    plan_path: Path,
    report_bindings: dict[str, Path],
) -> dict[str, Any]:
    plan_validator = _load_script(
        "validate-node-capacity-load-plan.py",
        "irlight_validate_node_capacity_load_plan_for_coverage",
    )
    report_validator = _load_script(
        "validate-node-capacity-report.py",
        "irlight_validate_node_capacity_report_for_coverage",
    )

    try:
        plan = plan_validator.load_plan(plan_path)
        plan_summary = plan_validator.validate_plan(plan)
    except plan_validator.PlanValidationError as exc:
        raise CapacityCoverageError("load plan is invalid") from exc

    required_ids = list(plan_summary["scenario_ids"])
    required_set = set(required_ids)
    supplied_set = set(report_bindings)
    missing_ids = [scenario_id for scenario_id in required_ids if scenario_id not in supplied_set]
    unknown_ids = sorted(supplied_set - required_set)
    if missing_ids or unknown_ids:
        details: list[str] = []
        if missing_ids:
            details.append(f"missing={','.join(missing_ids)}")
        if unknown_ids:
            details.append(f"unknown={','.join(unknown_ids)}")
        raise CapacityCoverageError(
            f"report bindings do not match plan scenarios ({'; '.join(details)})"
        )

    profile_label = plan_summary["profile_label"]
    common_node_profile: str | None = None
    common_revision: str | None = None
    common_margin: int | None = None
    seen_run_ids: set[str] = set()
    scenario_results: list[dict[str, Any]] = []

    scenarios_by_id = {scenario["id"]: scenario for scenario in plan["scenarios"]}
    for scenario_id in required_ids:
        report_summary = _validate_report(report_validator, report_bindings[scenario_id])

        if profile_label not in report_summary["scenario"]:
            raise CapacityCoverageError(
                f"scenario {scenario_id} report does not identify the plan profile label"
            )

        planned_levels = list(scenarios_by_id[scenario_id]["session_counts"])
        tested_levels = list(report_summary["tested_load_levels"])
        missing_levels = [level for level in planned_levels if level not in tested_levels]
        if missing_levels:
            raise CapacityCoverageError(
                f"scenario {scenario_id} is missing planned load levels: "
                + ",".join(str(level) for level in missing_levels)
            )

        run_id = report_summary["run_id"]
        if run_id in seen_run_ids:
            raise CapacityCoverageError("each plan scenario must use a distinct measured run_id")
        seen_run_ids.add(run_id)

        if common_node_profile is None:
            common_node_profile = report_summary["node_profile"]
            common_revision = report_summary["software_revision"]
            common_margin = report_summary["safety_margin_percent"]
        else:
            if report_summary["node_profile"] != common_node_profile:
                raise CapacityCoverageError("capacity reports use different node profiles")
            if report_summary["software_revision"] != common_revision:
                raise CapacityCoverageError("capacity reports use different software revisions")
            if report_summary["safety_margin_percent"] != common_margin:
                raise CapacityCoverageError("capacity reports use different safety margins")

        scenario_results.append(
            {
                "scenario_id": scenario_id,
                "run_id": run_id,
                "tested_load_levels": tested_levels,
                "highest_passing_sessions": report_summary["highest_passing_sessions"],
                "first_failing_sessions": report_summary["first_failing_sessions"],
                "recommended_max_sessions": report_summary["recommended_max_sessions"],
            }
        )

    if not scenario_results:  # Canonical plans currently always have scenarios.
        raise CapacityCoverageError("load plan contains no scenarios")

    recommended = min(
        result["recommended_max_sessions"] for result in scenario_results
    )
    return {
        "valid": True,
        "schema_version": 1,
        "profile_label": profile_label,
        "planned_session_counts": list(plan_summary["session_counts"]),
        "node_profile": common_node_profile,
        "software_revision": common_revision,
        "safety_margin_percent": common_margin,
        "scenario_results": scenario_results,
        "recommended_max_sessions": recommended,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path, help="canonical load-plan JSON")
    parser.add_argument(
        "--report",
        action="append",
        type=parse_report_binding,
        default=[],
        metavar="SCENARIO_ID=REPORT.json",
        help="Bind one canonical measured report to one plan scenario. Repeat for every scenario.",
    )
    parser.add_argument("--json", action="store_true", help="emit a deterministic JSON summary")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        bindings = normalize_report_bindings(args.report)
        summary = validate_coverage(args.plan, bindings)
    except (CapacityCoverageError, OSError) as exc:
        print(f"node capacity plan coverage invalid: {exc}", file=sys.stderr)
        return 2

    if args.json:
        json.dump(summary, sys.stdout, ensure_ascii=False, sort_keys=True, allow_nan=False)
        sys.stdout.write("\n")
    else:
        print(
            "node capacity plan coverage valid: "
            f"scenarios={len(summary['scenario_results'])} "
            f"recommended_max_sessions={summary['recommended_max_sessions']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
