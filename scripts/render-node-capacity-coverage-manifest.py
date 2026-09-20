#!/usr/bin/env python3
"""Render a validated durable Node-capacity coverage manifest.

Schema v1 remains available for compatibility and binds a canonical load plan to
one measured report per scenario. Supplying both ``--trials`` and
``--run-manifest`` bindings upgrades the output to schema v2, where each report
is also revalidated against the raw trials and runner provenance sidecar that
produced it. The renderer is read-only and never executes load or mutates
production capacity.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_VALIDATOR = Path(__file__).with_name(
    "validate-node-capacity-coverage-manifest.py"
)


class CoverageManifestRenderError(ValueError):
    """Raised when CLI bindings cannot produce canonical coverage evidence."""


def _load_manifest_validator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "irlight_node_capacity_manifest_renderer_validator",
        MANIFEST_VALIDATOR,
    )
    if spec is None or spec.loader is None:
        raise CoverageManifestRenderError("coverage manifest validator could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # pragma: no cover - repository packaging failure
        raise CoverageManifestRenderError(
            "coverage manifest validator could not be loaded"
        ) from exc
    return module


def _parse_binding(raw: str, metavar: str) -> tuple[str, str]:
    scenario_id, separator, evidence_path = raw.partition("=")
    if (
        not separator
        or not scenario_id
        or not evidence_path
        or scenario_id != scenario_id.strip()
        or evidence_path != evidence_path.strip()
        or any(character in scenario_id for character in ("\x00", "\n", "\r"))
        or any(character in evidence_path for character in ("\x00", "\n", "\r"))
    ):
        raise argparse.ArgumentTypeError(f"must use canonical one-line {metavar}")
    return scenario_id, evidence_path


def parse_report_binding(raw: str) -> tuple[str, str]:
    return _parse_binding(raw, "SCENARIO_ID=REPOSITORY_REPORT.json")


def parse_trials_binding(raw: str) -> tuple[str, str]:
    return _parse_binding(raw, "SCENARIO_ID=REPOSITORY_TRIALS.jsonl")


def parse_run_manifest_binding(raw: str) -> tuple[str, str]:
    return _parse_binding(raw, "SCENARIO_ID=REPOSITORY_RUN.json")


def normalize_bindings(
    bindings: Iterable[tuple[str, str]],
    *,
    label: str,
) -> dict[str, str]:
    result: dict[str, str] = {}
    for scenario_id, evidence_path in bindings:
        if scenario_id in result:
            raise CoverageManifestRenderError(f"duplicate {label} binding for scenario")
        result[scenario_id] = evidence_path
    return result


def normalize_report_bindings(
    bindings: Iterable[tuple[str, str]],
) -> dict[str, str]:
    result = normalize_bindings(bindings, label="report")
    if not result:
        raise CoverageManifestRenderError("at least one report binding is required")
    return result


def render_manifest(
    load_plan: str,
    report_bindings: Iterable[tuple[str, str]],
    *,
    trials_bindings: Iterable[tuple[str, str]] = (),
    run_manifest_bindings: Iterable[tuple[str, str]] = (),
    repo_root: Path = ROOT,
) -> dict[str, object]:
    if (
        not isinstance(load_plan, str)
        or not load_plan
        or load_plan != load_plan.strip()
        or any(character in load_plan for character in ("\x00", "\n", "\r"))
    ):
        raise CoverageManifestRenderError("load plan must be one canonical repository path")

    reports = normalize_report_bindings(report_bindings)
    trials = normalize_bindings(trials_bindings, label="trials")
    run_manifests = normalize_bindings(run_manifest_bindings, label="run manifest")
    wants_provenance = bool(trials or run_manifests)
    if wants_provenance:
        if set(trials) != set(reports) or set(run_manifests) != set(reports):
            raise CoverageManifestRenderError(
                "schema-v2 provenance bindings must exactly match report scenarios"
            )
        schema_version = 2
        report_entries = [
            {
                "scenario_id": scenario_id,
                "path": report_path,
                "trials_path": trials[scenario_id],
                "run_manifest_path": run_manifests[scenario_id],
            }
            for scenario_id, report_path in reports.items()
        ]
    else:
        schema_version = 1
        report_entries = [
            {"scenario_id": scenario_id, "path": report_path}
            for scenario_id, report_path in reports.items()
        ]

    payload: dict[str, object] = {
        "schema_version": schema_version,
        "load_plan": load_plan,
        "reports": report_entries,
    }

    validator = _load_manifest_validator()
    try:
        summary = validator.validate_manifest(payload, repo_root=repo_root)
    except (validator.CapacityCoverageManifestError, OSError, UnicodeError) as exc:
        raise CoverageManifestRenderError("coverage inputs are invalid") from exc

    scenario_ids = summary["scenario_ids"]
    entries_by_scenario = {entry["scenario_id"]: entry for entry in report_entries}
    canonical = {
        "schema_version": schema_version,
        "load_plan": load_plan,
        "reports": [entries_by_scenario[scenario_id] for scenario_id in scenario_ids],
    }

    # Defend against renderer/validator drift: only emit what the canonical
    # manifest validator itself accepts.
    try:
        validator.validate_manifest(canonical, repo_root=repo_root)
    except (validator.CapacityCoverageManifestError, OSError, UnicodeError) as exc:
        raise CoverageManifestRenderError("rendered coverage manifest is invalid") from exc
    return canonical


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "load_plan",
        help="canonical repository-relative Node-capacity load-plan JSON",
    )
    parser.add_argument(
        "--report",
        action="append",
        type=parse_report_binding,
        default=[],
        metavar="SCENARIO_ID=REPOSITORY_REPORT.json",
        help="Bind one measured report to one plan scenario. Repeat for every scenario.",
    )
    parser.add_argument(
        "--trials",
        action="append",
        type=parse_trials_binding,
        default=[],
        metavar="SCENARIO_ID=REPOSITORY_TRIALS.jsonl",
        help="Bind raw trials for schema v2. Must cover every --report scenario.",
    )
    parser.add_argument(
        "--run-manifest",
        action="append",
        type=parse_run_manifest_binding,
        default=[],
        metavar="SCENARIO_ID=REPOSITORY_RUN.json",
        help="Bind runner provenance for schema v2. Must cover every --report scenario.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        manifest = render_manifest(
            args.load_plan,
            args.report,
            trials_bindings=args.trials,
            run_manifest_bindings=args.run_manifest,
        )
    except CoverageManifestRenderError as exc:
        print(f"node capacity coverage manifest render failed: {exc}", file=sys.stderr)
        return 2

    json.dump(manifest, sys.stdout, indent=2, sort_keys=True, allow_nan=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
