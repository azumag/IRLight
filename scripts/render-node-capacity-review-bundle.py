#!/usr/bin/env python3
"""Render a digest-pinned review bundle for a validated Node-capacity proposal.

The existing max_sessions proposal is intentionally a readable derived artifact,
but its repository paths are provenance references rather than content pins. This
read-only command validates that proposal again and emits SHA-256 digests for the
exact proposal, coverage manifest, load plan, and measured reports a reviewer is
approving.

It never edits scheduler inventory or Node configuration, executes load, contacts
a provider, or chooses capacity policy.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PROPOSAL_VALIDATOR = Path(__file__).with_name(
    "validate-node-capacity-max-sessions-proposal.py"
)
COVERAGE_VALIDATOR = Path(__file__).with_name(
    "validate-node-capacity-coverage-manifest.py"
)
# Canonical report validation currently caps one measured report at 2 MiB. Keep
# the pinning guardrail above that while still bounding a post-validation
# replacement before hashing it.
MAX_PINNED_FILE_BYTES = 8 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024


class CapacityReviewBundleRenderError(ValueError):
    """Raised when exact reviewed capacity evidence cannot be pinned safely."""


def _load_module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise CapacityReviewBundleRenderError("capacity evidence validator could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # pragma: no cover - repository packaging failure
        raise CapacityReviewBundleRenderError(
            "capacity evidence validator could not be loaded"
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


def _identity(snapshot: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        snapshot.st_dev,
        snapshot.st_ino,
        snapshot.st_size,
        snapshot.st_mtime_ns,
        snapshot.st_ctime_ns,
    )


def _stable_sha256(path: Path) -> str:
    """Hash one bounded stable regular file without following a final symlink."""

    try:
        before = os.lstat(path)
    except OSError as exc:
        raise CapacityReviewBundleRenderError("capacity evidence could not be inspected") from exc
    if not stat.S_ISREG(before.st_mode):
        raise CapacityReviewBundleRenderError("capacity evidence must be a regular file")
    if before.st_size > MAX_PINNED_FILE_BYTES:
        raise CapacityReviewBundleRenderError("capacity evidence exceeds pinning size limit")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise CapacityReviewBundleRenderError("capacity evidence could not be opened") from exc

    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise CapacityReviewBundleRenderError("capacity evidence must be a regular file")
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise CapacityReviewBundleRenderError("capacity evidence changed while opening")
        if opened.st_size > MAX_PINNED_FILE_BYTES:
            raise CapacityReviewBundleRenderError("capacity evidence exceeds pinning size limit")

        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(fd, READ_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_PINNED_FILE_BYTES:
                raise CapacityReviewBundleRenderError(
                    "capacity evidence exceeds pinning size limit"
                )
            digest.update(chunk)

        after_read = os.fstat(fd)
        try:
            after_path = os.lstat(path)
        except OSError as exc:
            raise CapacityReviewBundleRenderError(
                "capacity evidence changed while hashing"
            ) from exc
        if not stat.S_ISREG(after_path.st_mode):
            raise CapacityReviewBundleRenderError("capacity evidence changed while hashing")
        if _identity(opened) != _identity(after_read) or _identity(opened) != _identity(after_path):
            raise CapacityReviewBundleRenderError("capacity evidence changed while hashing")
        return digest.hexdigest()
    finally:
        os.close(fd)


def _collect_pins(
    *,
    resolved_proposal: Path,
    proposal_name: str,
    validated_proposal: dict[str, Any],
    coverage_validator: ModuleType,
    repo_root: Path,
) -> dict[str, Any]:
    coverage_name = validated_proposal.get("coverage_manifest")
    if not isinstance(coverage_name, str) or not coverage_name:
        raise CapacityReviewBundleRenderError("validated proposal has no coverage reference")

    try:
        resolved_coverage = coverage_validator._validate_repo_file(  # noqa: SLF001
            repo_root,
            coverage_name,
            "coverage manifest",
        )
        coverage_digest_before = _stable_sha256(resolved_coverage)
        coverage_payload = coverage_validator.load_manifest(resolved_coverage)
        coverage_validator.validate_manifest(coverage_payload, repo_root=repo_root)
        coverage_digest_after = _stable_sha256(resolved_coverage)
    except CapacityReviewBundleRenderError:
        raise
    except (
        coverage_validator.CapacityCoverageManifestError,
        OSError,
        RuntimeError,
        UnicodeError,
        ValueError,
    ) as exc:
        raise CapacityReviewBundleRenderError("capacity evidence closure is invalid") from exc
    if coverage_digest_before != coverage_digest_after:
        raise CapacityReviewBundleRenderError("capacity evidence changed while being pinned")

    load_plan_name = coverage_payload["load_plan"]
    reports_payload = coverage_payload["reports"]
    try:
        resolved_plan = coverage_validator._validate_repo_file(  # noqa: SLF001
            repo_root,
            load_plan_name,
            "load plan",
        )
        reports: list[dict[str, str]] = []
        for entry in reports_payload:
            resolved_report = coverage_validator._validate_repo_file(  # noqa: SLF001
                repo_root,
                entry["path"],
                "report path",
            )
            reports.append(
                {
                    "scenario_id": entry["scenario_id"],
                    "path": entry["path"],
                    "sha256": _stable_sha256(resolved_report),
                }
            )
    except CapacityReviewBundleRenderError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise CapacityReviewBundleRenderError("capacity evidence closure is invalid") from exc

    return {
        "proposal_path": proposal_name,
        "proposal_sha256": _stable_sha256(resolved_proposal),
        "coverage_manifest": coverage_name,
        "coverage_manifest_sha256": coverage_digest_after,
        "load_plan": {
            "path": load_plan_name,
            "sha256": _stable_sha256(resolved_plan),
        },
        "reports": reports,
    }


def render_bundle(
    proposal_path: Path,
    *,
    expected_node_profile: object,
    expected_software_revision: object,
    repo_root: Path = ROOT,
) -> dict[str, Any]:
    proposal_validator = _load_module(
        PROPOSAL_VALIDATOR,
        "irlight_node_capacity_review_bundle_proposal_validator",
    )
    coverage_validator = _load_module(
        COVERAGE_VALIDATOR,
        "irlight_node_capacity_review_bundle_coverage_validator",
    )
    try:
        root = repo_root.resolve(strict=True)
        resolved_proposal = proposal_validator._repository_file(  # noqa: SLF001
            proposal_path,
            repo_root=repo_root,
        )
        proposal_name = resolved_proposal.relative_to(root).as_posix()
        validated = proposal_validator.validate_proposal_file(
            resolved_proposal,
            expected_node_profile=expected_node_profile,
            expected_software_revision=expected_software_revision,
            repo_root=repo_root,
        )
        pins_before = _collect_pins(
            resolved_proposal=resolved_proposal,
            proposal_name=proposal_name,
            validated_proposal=validated,
            coverage_validator=coverage_validator,
            repo_root=repo_root,
        )

        # Revalidate after hashing the complete evidence closure. This catches a
        # report/plan/manifest/proposal replacement that occurred between the
        # first canonical validation and digest collection.
        validated_after = proposal_validator.validate_proposal_file(
            resolved_proposal,
            expected_node_profile=expected_node_profile,
            expected_software_revision=expected_software_revision,
            repo_root=repo_root,
        )
        pins_after = _collect_pins(
            resolved_proposal=resolved_proposal,
            proposal_name=proposal_name,
            validated_proposal=validated_after,
            coverage_validator=coverage_validator,
            repo_root=repo_root,
        )
    except CapacityReviewBundleRenderError:
        raise
    except (
        proposal_validator.CapacityProposalValidationError,
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

    if _canonical_json(validated) != _canonical_json(validated_after):
        raise CapacityReviewBundleRenderError("capacity evidence changed while being pinned")
    if _canonical_json(pins_before) != _canonical_json(pins_after):
        raise CapacityReviewBundleRenderError("capacity evidence changed while being pinned")

    return {
        "schema_version": 1,
        **pins_after,
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
