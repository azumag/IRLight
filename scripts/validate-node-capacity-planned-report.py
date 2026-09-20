#!/usr/bin/env python3
"""Validate one persisted Node-capacity report against its run provenance.

A canonical capacity report can be schema-valid without proving that its measured
trials came from the canonical load plan and run sidecar introduced by the
scenario runner. This validator closes that local evidence gap by rebuilding the
report through ``assemble-node-capacity-planned-report.py`` from the canonical
plan, raw trials, and run manifest, then requiring exact JSON-value equality with
the persisted report.

The report's ``run_id``, ``safety_margin_percent``, and ``notes`` remain explicit
report metadata/policy inputs. Measured trials, media profile/scenario, Node
profile, and software revision are derived from the validated run provenance.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


class PlannedReportValidationError(ValueError):
    """Raised when a persisted report is not bound to canonical run provenance."""


def _load_script(filename: str, module_name: str) -> ModuleType:
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise PlannedReportValidationError("capacity provenance validator could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # pragma: no cover - repository packaging failure
        raise PlannedReportValidationError(
            "capacity provenance validator could not be loaded"
        ) from exc
    return module


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _identity(snapshot: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        snapshot.st_dev,
        snapshot.st_ino,
        snapshot.st_size,
        snapshot.st_mtime_ns,
        snapshot.st_ctime_ns,
    )


def _load_stable_report(path: Path, report_validator: ModuleType) -> dict[str, Any]:
    """Parse one bounded report while pinning the public path to the opened inode."""

    try:
        before = os.lstat(path)
    except OSError as exc:
        raise PlannedReportValidationError("persisted capacity report cannot be inspected") from exc
    if not stat.S_ISREG(before.st_mode):
        raise PlannedReportValidationError("persisted capacity report must be a regular file")
    if before.st_size > report_validator.MAX_REPORT_BYTES:
        raise PlannedReportValidationError("persisted capacity report exceeds size limit")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise PlannedReportValidationError("persisted capacity report cannot be opened") from exc

    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise PlannedReportValidationError("persisted capacity report must be a regular file")
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise PlannedReportValidationError("persisted capacity report changed while opening")
        if opened.st_size > report_validator.MAX_REPORT_BYTES:
            raise PlannedReportValidationError("persisted capacity report exceeds size limit")

        raw = os.read(fd, report_validator.MAX_REPORT_BYTES + 1)
        if len(raw) > report_validator.MAX_REPORT_BYTES:
            raise PlannedReportValidationError("persisted capacity report exceeds size limit")

        after_read = os.fstat(fd)
        try:
            after_path = os.lstat(path)
        except OSError as exc:
            raise PlannedReportValidationError(
                "persisted capacity report changed while reading"
            ) from exc
        if not stat.S_ISREG(after_path.st_mode):
            raise PlannedReportValidationError("persisted capacity report changed while reading")
        if _identity(opened) != _identity(after_read) or _identity(opened) != _identity(after_path):
            raise PlannedReportValidationError("persisted capacity report changed while reading")
    finally:
        os.close(fd)

    try:
        text = raw.decode("utf-8")
        value = json.loads(
            text,
            parse_constant=report_validator._reject_constant,  # noqa: SLF001
            object_pairs_hook=report_validator._strict_object,  # noqa: SLF001
        )
    except report_validator.CapacityReportError as exc:
        raise PlannedReportValidationError("persisted capacity report is invalid") from exc
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise PlannedReportValidationError("persisted capacity report is invalid") from exc
    if not isinstance(value, dict):
        raise PlannedReportValidationError("persisted capacity report is invalid")
    return value


def _validate_scenario_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(character in value for character in ("\x00", "\n", "\r"))
    ):
        raise PlannedReportValidationError("scenario must be one canonical line")
    return value


def validate_planned_report(
    *,
    report_path: Path,
    plan_path: Path,
    trials_path: Path,
    run_manifest_path: Path,
    scenario_id: str,
) -> dict[str, Any]:
    """Rebuild a persisted report from run provenance and require exact equality."""

    scenario_id = _validate_scenario_id(scenario_id)
    report_validator = _load_script(
        "validate-node-capacity-report.py",
        "irlight_node_capacity_report_validator_for_provenance",
    )
    planned_assembler = _load_script(
        "assemble-node-capacity-planned-report.py",
        "irlight_node_capacity_planned_assembler_for_provenance",
    )

    report = _load_stable_report(report_path, report_validator)
    try:
        summary = report_validator.validate_report(report)
    except report_validator.CapacityReportError as exc:
        raise PlannedReportValidationError("persisted capacity report is invalid") from exc

    try:
        rebuilt = planned_assembler.assemble_planned_report(
            plan_path=plan_path,
            trials_path=trials_path,
            run_manifest_path=run_manifest_path,
            scenario_id=scenario_id,
            run_id=report["run_id"],
            safety_margin_percent=report["safety_margin_percent"],
            notes=report["notes"],
        )
    except planned_assembler.PlannedReportError as exc:
        raise PlannedReportValidationError("capacity run provenance is invalid") from exc

    if _canonical_json(report) != _canonical_json(rebuilt):
        raise PlannedReportValidationError(
            "persisted capacity report does not match canonical run provenance"
        )

    return {
        **summary,
        "provenance_bound": True,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="persisted canonical capacity report JSON")
    parser.add_argument("--plan", type=Path, required=True, help="canonical load-plan JSON")
    parser.add_argument(
        "--trials-jsonl",
        type=Path,
        required=True,
        help="raw trial JSONL emitted by the scenario runner",
    )
    parser.add_argument(
        "--run-manifest",
        type=Path,
        required=True,
        help="run provenance sidecar emitted by the scenario runner",
    )
    parser.add_argument("--scenario", required=True, help="canonical scenario ID")
    parser.add_argument("--json", action="store_true", help="emit deterministic JSON summary")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        summary = validate_planned_report(
            report_path=args.report,
            plan_path=args.plan,
            trials_path=args.trials_jsonl,
            run_manifest_path=args.run_manifest,
            scenario_id=args.scenario,
        )
    except (PlannedReportValidationError, OSError, UnicodeError, ValueError, RecursionError) as exc:
        # Paths, profile labels, and scenario IDs are operator/evidence controlled.
        # All validation errors above are intentionally fixed strings.
        print(f"planned Node capacity report invalid: {exc}", file=sys.stderr)
        return 2

    if args.json:
        json.dump(summary, sys.stdout, ensure_ascii=False, sort_keys=True, allow_nan=False)
        sys.stdout.write("\n")
    else:
        print(
            "planned Node capacity report valid: "
            f"recommended_max_sessions={summary['recommended_max_sessions']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
