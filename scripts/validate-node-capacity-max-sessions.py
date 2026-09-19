#!/usr/bin/env python3
"""Validate a proposed Media Node max_sessions against complete measured coverage.

This is a read-only decision guardrail. It validates the durable Node-capacity
coverage manifest with the repository's canonical validator, binds that
evidence to an explicitly supplied Node profile and software revision, then
rejects a proposed max_sessions value that exceeds the conservative
recommendation supported by that complete scenario set. It never edits
scheduler inventory, chooses a safety margin, executes load, or contacts a
provider.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
COVERAGE_MANIFEST_VALIDATOR = Path(__file__).with_name(
    "validate-node-capacity-coverage-manifest.py"
)


class CapacityMaxSessionsError(ValueError):
    """Raised when a proposed max_sessions is not supported by measured evidence."""


def _load_coverage_manifest_validator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "irlight_node_capacity_manifest_for_max_sessions",
        COVERAGE_MANIFEST_VALIDATOR,
    )
    if spec is None or spec.loader is None:
        raise CapacityMaxSessionsError("coverage manifest validator could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # pragma: no cover - repository packaging failure
        raise CapacityMaxSessionsError(
            "coverage manifest validator could not be loaded"
        ) from exc
    return module


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CapacityMaxSessionsError(f"{label} must be a positive integer")
    return value


def _expected_node_profile(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 300:
        raise CapacityMaxSessionsError(
            "expected node_profile must be a non-empty string up to 300 characters"
        )
    return value


def _expected_revision(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 40
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise CapacityMaxSessionsError(
            "expected software_revision must be a lowercase 40-character Git commit SHA"
        )
    return value


def validate_max_sessions(
    manifest_path: Path,
    candidate_max_sessions: object,
    *,
    expected_node_profile: object,
    expected_software_revision: object,
    repo_root: Path = ROOT,
) -> dict[str, Any]:
    candidate = _positive_int(candidate_max_sessions, "candidate max_sessions")
    target_profile = _expected_node_profile(expected_node_profile)
    target_revision = _expected_revision(expected_software_revision)

    manifest_validator = _load_coverage_manifest_validator()
    try:
        coverage = manifest_validator.validate_manifest_file(
            manifest_path,
            repo_root=repo_root,
        )
    except manifest_validator.CapacityCoverageManifestError as exc:
        # Evidence-controlled paths/scenario labels must not be reflected into
        # operator/CI stderr through this higher-level guardrail.
        raise CapacityMaxSessionsError("coverage manifest is invalid") from exc

    recommendation = _positive_int(
        coverage.get("recommended_max_sessions"),
        "measured recommendation",
    )
    report_count = _positive_int(coverage.get("report_count"), "coverage report_count")
    margin = _positive_int(
        coverage.get("safety_margin_percent"),
        "coverage safety_margin_percent",
    )
    revision = coverage.get("software_revision")
    node_profile = coverage.get("node_profile")
    if not isinstance(revision, str) or not revision:
        raise CapacityMaxSessionsError("coverage software_revision is invalid")
    if not isinstance(node_profile, str) or not node_profile:
        raise CapacityMaxSessionsError("coverage node_profile is invalid")

    if node_profile != target_profile:
        raise CapacityMaxSessionsError(
            "coverage node_profile does not match the proposed deployment"
        )
    if revision != target_revision:
        raise CapacityMaxSessionsError(
            "coverage software_revision does not match the proposed deployment"
        )
    if candidate > recommendation:
        raise CapacityMaxSessionsError(
            "candidate max_sessions exceeds the measured recommendation"
        )

    return {
        "valid": True,
        "candidate_max_sessions": candidate,
        "recommended_max_sessions": recommendation,
        "headroom_sessions": recommendation - candidate,
        "report_count": report_count,
        "safety_margin_percent": margin,
        "software_revision": revision,
        "node_profile": node_profile,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "manifest",
        type=Path,
        help="durable Node-capacity coverage manifest",
    )
    parser.add_argument(
        "--max-sessions",
        required=True,
        type=int,
        help="proposed positive scheduler/Node inventory max_sessions",
    )
    parser.add_argument(
        "--node-profile",
        required=True,
        help="exact Node profile of the proposed deployment",
    )
    parser.add_argument(
        "--software-revision",
        required=True,
        help="exact lowercase 40-character Git commit SHA of the proposed deployment",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit a deterministic JSON summary",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        summary = validate_max_sessions(
            args.manifest,
            args.max_sessions,
            expected_node_profile=args.node_profile,
            expected_software_revision=args.software_revision,
        )
    except (CapacityMaxSessionsError, OSError) as exc:
        print(f"node capacity max_sessions invalid: {exc}", file=sys.stderr)
        return 2

    if args.json:
        json.dump(summary, sys.stdout, ensure_ascii=False, sort_keys=True, allow_nan=False)
        sys.stdout.write("\n")
    else:
        print(
            "node capacity max_sessions valid: "
            f"candidate={summary['candidate_max_sessions']} "
            f"measured_recommendation={summary['recommended_max_sessions']} "
            f"headroom={summary['headroom_sessions']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
