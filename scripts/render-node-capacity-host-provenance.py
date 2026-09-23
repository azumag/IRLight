#!/usr/bin/env python3
"""Render a digest-pinned Node-capacity host-provenance closure.

The input coverage manifest must already use provenance-bound schema v2. Each
scenario is additionally bound to one persisted host-preflight JSON file. The
renderer validates all inputs before emitting a sidecar and never executes load,
contacts providers, or changes production capacity.
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
PROVENANCE_VALIDATOR = Path(__file__).with_name(
    "validate-node-capacity-host-provenance.py"
)
COVERAGE_VALIDATOR = Path(__file__).with_name(
    "validate-node-capacity-coverage-manifest.py"
)


class HostProvenanceRenderError(ValueError):
    """Raised when inputs cannot produce canonical host provenance evidence."""


def _load_module(path: Path, module_name: str, label: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise HostProvenanceRenderError(f"{label} could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # pragma: no cover - repository packaging failure
        raise HostProvenanceRenderError(f"{label} could not be loaded") from exc
    return module


def _load_provenance_validator() -> ModuleType:
    return _load_module(
        PROVENANCE_VALIDATOR,
        "irlight_node_capacity_host_provenance_renderer_validator",
        "host provenance validator",
    )


def _load_coverage_validator() -> ModuleType:
    return _load_module(
        COVERAGE_VALIDATOR,
        "irlight_node_capacity_host_provenance_renderer_coverage_validator",
        "coverage validator",
    )


def parse_preflight_binding(raw: str) -> tuple[str, str]:
    scenario_id, separator, path = raw.partition("=")
    if (
        not separator
        or not scenario_id
        or not path
        or scenario_id != scenario_id.strip()
        or path != path.strip()
        or any(character in scenario_id for character in ("\x00", "\n", "\r"))
        or any(character in path for character in ("\x00", "\n", "\r"))
    ):
        raise argparse.ArgumentTypeError(
            "must use canonical one-line SCENARIO_ID=REPOSITORY_PREFLIGHT.json"
        )
    return scenario_id, path


def _normalize_preflights(
    bindings: Iterable[tuple[str, str]],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for scenario_id, path in bindings:
        if scenario_id in result:
            raise HostProvenanceRenderError("duplicate host preflight binding for scenario")
        result[scenario_id] = path
    if not result:
        raise HostProvenanceRenderError("at least one host preflight binding is required")
    return result


def render_manifest(
    coverage_manifest: str,
    preflight_bindings: Iterable[tuple[str, str]],
    *,
    repo_root: Path = ROOT,
) -> dict[str, object]:
    coverage = _load_coverage_validator()
    provenance = _load_provenance_validator()

    try:
        coverage_text, coverage_path = provenance._validated_repo_path(
            repo_root,
            coverage_manifest,
            label="coverage manifest path",
            coverage_validator=coverage,
        )
        coverage_payload = coverage.load_manifest(coverage_path)
        coverage_summary = coverage.validate_manifest(coverage_payload, repo_root=repo_root)
    except (
        provenance.HostProvenanceError,
        coverage.CapacityCoverageManifestError,
        OSError,
        UnicodeError,
    ) as exc:
        raise HostProvenanceRenderError("coverage manifest is invalid") from exc

    if coverage_summary.get("schema_version") != 2 or coverage_summary.get("provenance_bound") is not True:
        raise HostProvenanceRenderError("coverage manifest must use provenance-bound schema v2")

    preflights = _normalize_preflights(preflight_bindings)
    scenario_ids = coverage_summary["scenario_ids"]
    if set(preflights) != set(scenario_ids):
        raise HostProvenanceRenderError(
            "host preflight bindings must exactly match coverage scenarios"
        )

    coverage_entries = {
        entry["scenario_id"]: entry
        for entry in coverage_payload["reports"]
    }
    try:
        payload: dict[str, object] = {
            "schema_version": 1,
            "coverage": provenance.make_pin(
                repo_root,
                coverage_text,
                label="coverage manifest",
                coverage_validator=coverage,
            ),
            "load_plan": provenance.make_pin(
                repo_root,
                coverage_payload["load_plan"],
                label="load plan",
                coverage_validator=coverage,
            ),
            "scenarios": [],
        }
        scenarios: list[dict[str, object]] = []
        for scenario_id in scenario_ids:
            entry = coverage_entries[scenario_id]
            scenarios.append(
                {
                    "scenario_id": scenario_id,
                    "host_preflight": provenance.make_preflight_pin(
                        repo_root,
                        preflights[scenario_id],
                        coverage_validator=coverage,
                    ),
                    "report": provenance.make_pin(
                        repo_root,
                        entry["path"],
                        label="report evidence",
                        coverage_validator=coverage,
                    ),
                    "trials": provenance.make_pin(
                        repo_root,
                        entry["trials_path"],
                        label="trials evidence",
                        coverage_validator=coverage,
                    ),
                    "run_manifest": provenance.make_pin(
                        repo_root,
                        entry["run_manifest_path"],
                        label="run manifest evidence",
                        coverage_validator=coverage,
                    ),
                }
            )
        payload["scenarios"] = scenarios
        provenance.validate_manifest(payload, repo_root=repo_root)
    except (provenance.HostProvenanceError, OSError, UnicodeError) as exc:
        raise HostProvenanceRenderError("host provenance inputs are invalid") from exc
    return payload


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "coverage_manifest",
        help="repository-relative provenance-bound coverage-v2 JSON",
    )
    parser.add_argument(
        "--preflight",
        action="append",
        type=parse_preflight_binding,
        default=[],
        metavar="SCENARIO_ID=REPOSITORY_PREFLIGHT.json",
        help="Bind one validated host preflight to one measured scenario. Repeat for every scenario.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        manifest = render_manifest(args.coverage_manifest, args.preflight)
    except HostProvenanceRenderError as exc:
        print(f"node capacity host provenance render failed: {exc}", file=sys.stderr)
        return 2

    json.dump(manifest, sys.stdout, indent=2, sort_keys=True, allow_nan=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
