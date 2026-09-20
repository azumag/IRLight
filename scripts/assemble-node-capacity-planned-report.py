#!/usr/bin/env python3
"""Assemble Node-capacity evidence only when it covers one canonical scenario plan.

The lower-level ``assemble-node-capacity-report.py`` intentionally validates the
trial/report schema without knowing which load plan produced the measurements.
This entry point requires the runner's provenance sidecar, validates its digests
and measured Node/software identity, and requires the raw trial concurrency
ladder to exactly match the selected canonical Issue #13 scenario before a
report can be emitted.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


RUN_MANIFEST_FIELDS = {
    "schema_version",
    "plan_sha256",
    "profile_label",
    "scenario_id",
    "node_profile",
    "software_revision",
    "failure_policy",
    "planned_load_levels",
    "tested_load_levels",
    "boundary_found",
    "completed_plan",
    "trials_sha256",
}


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


def _reject_constant(_value: str) -> None:
    raise PlannedReportError("run manifest contains a non-standard JSON number")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PlannedReportError("run manifest contains a duplicate JSON key")
        result[key] = value
    return result


def _canonical_digest(value: Any) -> str:
    rendered = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(rendered).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_revision(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 40
        and all(character in "0123456789abcdef" for character in value)
    )


def _positive_int_list(value: object) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(
            isinstance(item, int) and not isinstance(item, bool) and item > 0
            for item in value
        )
    )


def _load_run_manifest(path: Path, plan_validator: ModuleType) -> dict[str, Any]:
    # Reuse the plan validator's bounded, stable, regular-file reader. The sidecar
    # is smaller than that reader's 256 KiB cap and receives independent schema
    # validation below.
    try:
        raw = plan_validator._read_plan_bytes(path).decode("utf-8")
    except (plan_validator.PlanValidationError, UnicodeDecodeError, OSError) as exc:
        raise PlannedReportError("run manifest cannot be read safely") from exc
    try:
        value = json.loads(
            raw,
            parse_constant=_reject_constant,
            object_pairs_hook=_strict_object,
        )
    except PlannedReportError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise PlannedReportError("run manifest is not valid JSON") from exc
    if not isinstance(value, dict) or set(value) != RUN_MANIFEST_FIELDS:
        raise PlannedReportError("run manifest has an unexpected shape")
    schema_version = value["schema_version"]
    if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version != 1:
        raise PlannedReportError("run manifest has an unsupported schema version")
    if not _is_sha256(value["plan_sha256"]) or not _is_sha256(value["trials_sha256"]):
        raise PlannedReportError("run manifest has an invalid digest")
    if not isinstance(value["profile_label"], str) or not value["profile_label"]:
        raise PlannedReportError("run manifest has an invalid profile label")
    if not isinstance(value["scenario_id"], str) or not value["scenario_id"]:
        raise PlannedReportError("run manifest has an invalid scenario id")
    node_profile = value["node_profile"]
    if (
        not isinstance(node_profile, str)
        or not node_profile.strip()
        or len(node_profile) > 300
    ):
        raise PlannedReportError("run manifest has an invalid node profile")
    if not _is_revision(value["software_revision"]):
        raise PlannedReportError("run manifest has an invalid software revision")
    failure_policy = value["failure_policy"]
    if not isinstance(failure_policy, str) or failure_policy not in {"stop", "continue"}:
        raise PlannedReportError("run manifest has an invalid failure policy")
    if not _positive_int_list(value["planned_load_levels"]):
        raise PlannedReportError("run manifest has invalid planned load levels")
    if not _positive_int_list(value["tested_load_levels"]):
        raise PlannedReportError("run manifest has invalid tested load levels")
    if not isinstance(value["boundary_found"], bool) or not isinstance(
        value["completed_plan"], bool
    ):
        raise PlannedReportError("run manifest has invalid completion flags")
    return value


def _validate_run_manifest(
    manifest: dict[str, Any],
    *,
    plan: dict[str, Any],
    scenario_id: str,
    planned_levels: list[int],
    normalized_trials: list[dict[str, Any]],
) -> None:
    measured_levels = [trial["concurrent_sessions"] for trial in normalized_trials]
    failure_seen = any(trial["outcome"] == "fail" for trial in normalized_trials)
    if manifest["plan_sha256"] != _canonical_digest(plan):
        raise PlannedReportError("run manifest does not match the canonical load plan")
    if manifest["trials_sha256"] != _canonical_digest(normalized_trials):
        raise PlannedReportError("run manifest does not match the raw trials")
    if manifest["profile_label"] != plan["profile_label"]:
        raise PlannedReportError("run manifest does not match the plan profile")
    if manifest["scenario_id"] != scenario_id:
        raise PlannedReportError("run manifest does not match the selected scenario")
    if manifest["planned_load_levels"] != planned_levels:
        raise PlannedReportError("run manifest does not match the planned load levels")
    if manifest["tested_load_levels"] != measured_levels:
        raise PlannedReportError("run manifest does not match the measured load levels")
    if manifest["boundary_found"] is not failure_seen:
        raise PlannedReportError("run manifest does not match the measured boundary")
    if manifest["completed_plan"] is not True:
        raise PlannedReportError("run manifest does not represent a completed scenario plan")
    if measured_levels != planned_levels:
        raise PlannedReportError(
            "raw trials do not cover the complete canonical scenario plan"
        )
    if manifest["failure_policy"] == "stop" and failure_seen:
        first_failure_index = next(
            index
            for index, trial in enumerate(normalized_trials)
            if trial["outcome"] == "fail"
        )
        if first_failure_index != len(normalized_trials) - 1:
            raise PlannedReportError(
                "run manifest failure policy is inconsistent with the measured boundary"
            )


def assemble_planned_report(
    *,
    plan_path: Path,
    trials_path: Path,
    run_manifest_path: Path,
    scenario_id: str,
    run_id: str,
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

    planned_levels = list(scenario["session_counts"])
    manifest = _load_run_manifest(run_manifest_path, plan_validator)
    _validate_run_manifest(
        manifest,
        plan=plan,
        scenario_id=scenario_id,
        planned_levels=planned_levels,
        normalized_trials=normalized,
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
            node_profile=manifest["node_profile"],
            software_revision=manifest["software_revision"],
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
    parser.add_argument("--run-manifest", type=Path, required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--run-id", required=True)
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
            run_manifest_path=args.run_manifest,
            scenario_id=args.scenario,
            run_id=args.run_id,
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
