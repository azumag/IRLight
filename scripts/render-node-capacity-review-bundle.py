#!/usr/bin/env python3
"""Render a digest-pinned review bundle for a validated Node-capacity proposal.

The existing max_sessions proposal is intentionally a readable derived artifact,
but its repository paths are provenance references rather than content pins. This
read-only command validates that proposal again and emits SHA-256 digests for the
exact proposal and coverage-manifest bytes a reviewer is approving.

It never edits scheduler inventory or Node configuration, executes load, contacts
a provider, or chooses capacity policy.
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

ROOT = Path(__file__).resolve().parents[1]
PROPOSAL_VALIDATOR = Path(__file__).with_name(
    "validate-node-capacity-max-sessions-proposal.py"
)


class CapacityReviewBundleRenderError(ValueError):
    """Raised when exact reviewed capacity evidence cannot be pinned safely."""


def _load_proposal_validator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "irlight_node_capacity_review_bundle_proposal_validator",
        PROPOSAL_VALIDATOR,
    )
    if spec is None or spec.loader is None:
        raise CapacityReviewBundleRenderError("proposal validator could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # pragma: no cover - repository packaging failure
        raise CapacityReviewBundleRenderError(
            "proposal validator could not be loaded"
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


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def render_bundle(
    proposal_path: Path,
    *,
    expected_node_profile: object,
    expected_software_revision: object,
    repo_root: Path = ROOT,
) -> dict[str, Any]:
    validator = _load_proposal_validator()
    try:
        root = repo_root.resolve(strict=True)
        resolved_proposal = validator._repository_file(  # noqa: SLF001 - sibling CLI contract
            proposal_path,
            repo_root=repo_root,
        )
        proposal_name = resolved_proposal.relative_to(root).as_posix()

        # Read before and after canonical validation. The validator itself also
        # performs stable regular-file reads; the repeated byte comparison here
        # ensures the digest is for the same proposal that passed validation.
        proposal_before = validator._read_stable_bytes(resolved_proposal)  # noqa: SLF001
        validated = validator.validate_proposal_file(
            resolved_proposal,
            expected_node_profile=expected_node_profile,
            expected_software_revision=expected_software_revision,
            repo_root=repo_root,
        )

        coverage_name = validated["coverage_manifest"]
        if not isinstance(coverage_name, str) or not coverage_name:
            raise CapacityReviewBundleRenderError("validated proposal has no coverage reference")
        resolved_coverage = validator._repository_file(  # noqa: SLF001
            repo_root / coverage_name,
            repo_root=repo_root,
        )
        coverage_before = validator._read_stable_bytes(resolved_coverage)  # noqa: SLF001

        validated_after = validator.validate_proposal_file(
            resolved_proposal,
            expected_node_profile=expected_node_profile,
            expected_software_revision=expected_software_revision,
            repo_root=repo_root,
        )
        proposal_after = validator._read_stable_bytes(resolved_proposal)  # noqa: SLF001
        coverage_after = validator._read_stable_bytes(resolved_coverage)  # noqa: SLF001
    except CapacityReviewBundleRenderError:
        raise
    except (
        validator.CapacityProposalValidationError,
        OSError,
        RuntimeError,
        UnicodeError,
        ValueError,
    ) as exc:
        # Node/profile labels and evidence paths are operator-controlled. Keep
        # errors fixed rather than reflecting those values into CI/operator logs.
        raise CapacityReviewBundleRenderError(
            "proposal, evidence, or deployment identity is invalid"
        ) from exc

    if proposal_before != proposal_after or coverage_before != coverage_after:
        raise CapacityReviewBundleRenderError("capacity evidence changed while being pinned")
    if _canonical_json(validated) != _canonical_json(validated_after):
        raise CapacityReviewBundleRenderError("capacity evidence changed while being pinned")

    return {
        "schema_version": 1,
        "proposal_path": proposal_name,
        "proposal_sha256": _sha256(proposal_after),
        "coverage_manifest": coverage_name,
        "coverage_manifest_sha256": _sha256(coverage_after),
        "node_profile": validated_after["node_profile"],
        "software_revision": validated_after["software_revision"],
        "candidate_max_sessions": validated_after["candidate_max_sessions"],
        "measured_recommended_max_sessions": validated_after[
            "measured_recommended_max_sessions"
        ],
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "proposal",
        type=Path,
        help="repository-relative persisted Node-capacity max_sessions proposal",
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
        bundle = render_bundle(
            args.proposal,
            expected_node_profile=args.node_profile,
            expected_software_revision=args.software_revision,
        )
    except CapacityReviewBundleRenderError as exc:
        print(f"node capacity review bundle render failed: {exc}", file=sys.stderr)
        return 2

    json.dump(bundle, sys.stdout, indent=2, sort_keys=True, allow_nan=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
