#!/usr/bin/env python3
"""Validate a persisted digest-pinned Node-capacity review bundle.

The bundle is not authority for production configuration. This read-only check
re-renders it from the referenced persisted max_sessions proposal and current
coverage closure, verifies the explicit deployment identity, and requires exact
canonical equality. Any byte change to the pinned proposal, coverage manifest,
load plan, measured reports, or (for schema v2+) raw trials/run manifests after
review therefore invalidates the bundle even when the JSON meaning is unchanged.
Schema v3 additionally revalidates and pins the host-provenance sidecar and the
exact host-preflight evidence associated with every measured scenario.
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
PROPOSAL_VALIDATOR = Path(__file__).with_name(
    "validate-node-capacity-max-sessions-proposal.py"
)
BUNDLE_RENDERER = Path(__file__).with_name("render-node-capacity-review-bundle.py")
BASE_BUNDLE_FIELDS = {
    "schema_version",
    "proposal_path",
    "proposal_sha256",
    "coverage_manifest",
    "coverage_manifest_sha256",
    "load_plan",
    "reports",
    "node_profile",
    "software_revision",
    "candidate_max_sessions",
    "measured_recommended_max_sessions",
}
HOST_PROVENANCE_FIELDS = {"path", "sha256", "scenarios"}


class CapacityReviewBundleValidationError(ValueError):
    """Raised when a persisted review bundle is unsafe, stale, or modified."""


def _load_module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise CapacityReviewBundleValidationError("review bundle dependency could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # pragma: no cover - repository packaging failure
        raise CapacityReviewBundleValidationError(
            "review bundle dependency could not be loaded"
        ) from exc
    return module


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def validate_bundle_file(
    bundle_path: Path,
    *,
    expected_node_profile: object,
    expected_software_revision: object,
    repo_root: Path = ROOT,
) -> dict[str, Any]:
    proposal_validator = _load_module(
        PROPOSAL_VALIDATOR,
        "irlight_node_capacity_review_bundle_input_validator",
    )
    try:
        resolved_bundle = proposal_validator._repository_file(  # noqa: SLF001
            bundle_path,
            repo_root=repo_root,
        )
        payload = proposal_validator._load_proposal(resolved_bundle)  # noqa: SLF001
    except (
        proposal_validator.CapacityProposalValidationError,
        OSError,
        RuntimeError,
        UnicodeError,
        ValueError,
    ) as exc:
        raise CapacityReviewBundleValidationError(
            "review bundle could not be read safely"
        ) from exc

    schema_version = payload.get("schema_version") if isinstance(payload, dict) else None
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version not in {1, 2, 3}
    ):
        raise CapacityReviewBundleValidationError("review bundle schema_version is unsupported")

    expected_fields = (
        BASE_BUNDLE_FIELDS | {"host_provenance"}
        if schema_version == 3
        else BASE_BUNDLE_FIELDS
    )
    if set(payload) != expected_fields:
        raise CapacityReviewBundleValidationError("review bundle has an unexpected top-level shape")

    proposal_name = payload["proposal_path"]
    if not isinstance(proposal_name, str) or not proposal_name:
        raise CapacityReviewBundleValidationError("review bundle proposal reference is invalid")

    host_provenance_path: Path | None = None
    if schema_version == 3:
        host_provenance = payload.get("host_provenance")
        if not isinstance(host_provenance, dict) or set(host_provenance) != HOST_PROVENANCE_FIELDS:
            raise CapacityReviewBundleValidationError(
                "review bundle host provenance has an unexpected shape"
            )
        path_text = host_provenance.get("path")
        if not isinstance(path_text, str) or not path_text:
            raise CapacityReviewBundleValidationError(
                "review bundle host provenance reference is invalid"
            )
        if not isinstance(host_provenance.get("scenarios"), list):
            raise CapacityReviewBundleValidationError(
                "review bundle host provenance scenario mapping is invalid"
            )
        host_provenance_path = Path(path_text)

    renderer = _load_module(
        BUNDLE_RENDERER,
        "irlight_node_capacity_review_bundle_validator_renderer",
    )
    try:
        expected = renderer.render_bundle(
            repo_root / proposal_name,
            expected_node_profile=expected_node_profile,
            expected_software_revision=expected_software_revision,
            host_provenance_path=host_provenance_path,
            repo_root=repo_root,
        )
    except (renderer.CapacityReviewBundleRenderError, OSError, UnicodeError) as exc:
        raise CapacityReviewBundleValidationError(
            "review bundle evidence or deployment identity is invalid"
        ) from exc

    try:
        matches = _canonical_json(payload) == _canonical_json(expected)
    except (TypeError, ValueError) as exc:
        raise CapacityReviewBundleValidationError(
            "review bundle contains an invalid value"
        ) from exc
    if not matches:
        raise CapacityReviewBundleValidationError(
            "review bundle does not match current pinned capacity evidence"
        )
    return expected


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "bundle",
        type=Path,
        help="repository-relative persisted Node-capacity review bundle",
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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        bundle = validate_bundle_file(
            args.bundle,
            expected_node_profile=args.node_profile,
            expected_software_revision=args.software_revision,
        )
    except CapacityReviewBundleValidationError as exc:
        print(f"node capacity review bundle invalid: {exc}", file=sys.stderr)
        return 2

    print(
        "node capacity review bundle valid: "
        f"candidate={bundle['candidate_max_sessions']} "
        f"measured_recommendation={bundle['measured_recommended_max_sessions']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
