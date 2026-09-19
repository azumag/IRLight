#!/usr/bin/env python3
"""Render a validated durable Node-capacity coverage manifest.

The renderer is read-only: it validates one canonical load plan and one measured
report per scenario, then emits the schema-v1 repository-relative manifest used
by release acceptance. It does not execute load, choose thresholds or a safety
margin, or mutate production capacity.
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


def parse_report_binding(raw: str) -> tuple[str, str]:
    scenario_id, separator, report_path = raw.partition("=")
    if (
        not separator
        or not scenario_id
        or not report_path
        or scenario_id != scenario_id.strip()
        or report_path != report_path.strip()
        or any(character in scenario_id for character in ("\x00", "\n", "\r"))
        or any(character in report_path for character in ("\x00", "\n", "\r"))
    ):
        raise argparse.ArgumentTypeError(
            "must use canonical one-line SCENARIO_ID=REPOSITORY_REPORT.json"
        )
    return scenario_id, report_path


def normalize_report_bindings(
    bindings: Iterable[tuple[str, str]],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for scenario_id, report_path in bindings:
        if scenario_id in result:
            # Scenario IDs are evidence-controlled. Keep operator/CI errors fixed.
            raise CoverageManifestRenderError("duplicate report binding for scenario")
        result[scenario_id] = report_path
    if not result:
        raise CoverageManifestRenderError("at least one report binding is required")
    return result


def render_manifest(
    load_plan: str,
    report_bindings: Iterable[tuple[str, str]],
    *,
    repo_root: Path = ROOT,
) -> dict[str, object]:
    if (
        not isinstance(load_plan, str)
        or not load_plan
        or load_plan != load_plan.strip()
        or any(character in load_plan for character in ("\x00", "\n", "\r"))
    ):
        raise CoverageManifestRenderError("load plan must be one canonical repository path")

    bindings = normalize_report_bindings(report_bindings)
    payload: dict[str, object] = {
        "schema_version": 1,
        "load_plan": load_plan,
        "reports": [
            {"scenario_id": scenario_id, "path": report_path}
            for scenario_id, report_path in bindings.items()
        ],
    }

    validator = _load_manifest_validator()
    try:
        summary = validator.validate_manifest(payload, repo_root=repo_root)
    except (validator.CapacityCoverageManifestError, OSError, UnicodeError) as exc:
        # Do not reflect evidence-controlled IDs or paths into stderr. The canonical
        # validator already carries detailed causes for tests and programmatic use.
        raise CoverageManifestRenderError("coverage inputs are invalid") from exc

    scenario_ids = summary["scenario_ids"]
    canonical = {
        "schema_version": 1,
        "load_plan": load_plan,
        "reports": [
            {"scenario_id": scenario_id, "path": bindings[scenario_id]}
            for scenario_id in scenario_ids
        ],
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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        manifest = render_manifest(args.load_plan, args.report)
    except CoverageManifestRenderError as exc:
        print(f"node capacity coverage manifest render failed: {exc}", file=sys.stderr)
        return 2

    json.dump(manifest, sys.stdout, indent=2, sort_keys=True, allow_nan=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
