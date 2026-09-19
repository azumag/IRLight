#!/usr/bin/env python3
"""Render a reviewable max_sessions proposal from complete measured coverage.

This command is read-only. It reuses the canonical max_sessions guardrail,
requires the coverage manifest itself to live at a canonical repository path,
and emits a deterministic schema-v1 proposal that can be committed alongside
the evidence used for review. It never edits scheduler inventory or Node
configuration, executes load, contacts a provider, or chooses capacity policy.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MAX_SESSIONS_VALIDATOR = Path(__file__).with_name(
    "validate-node-capacity-max-sessions.py"
)


class CapacityProposalRenderError(ValueError):
    """Raised when a durable max_sessions proposal cannot be rendered safely."""


def _load_max_sessions_validator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "irlight_node_capacity_max_sessions_proposal_validator",
        MAX_SESSIONS_VALIDATOR,
    )
    if spec is None or spec.loader is None:
        raise CapacityProposalRenderError("max_sessions validator could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # pragma: no cover - repository packaging failure
        raise CapacityProposalRenderError(
            "max_sessions validator could not be loaded"
        ) from exc
    return module


def _repository_manifest_path(path: Path, *, repo_root: Path) -> tuple[Path, str]:
    """Return a symlink-free manifest path and canonical repository-relative name."""

    try:
        root = repo_root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CapacityProposalRenderError("repository root could not be resolved") from exc

    try:
        if path.is_absolute():
            candidate = path
            relative = candidate.relative_to(root)
        else:
            relative = path
            candidate = root / relative
    except ValueError as exc:
        raise CapacityProposalRenderError(
            "coverage manifest must be a repository file"
        ) from exc

    if (
        not relative.parts
        or any(part in ("", ".", "..") for part in relative.parts)
        or relative.is_absolute()
    ):
        raise CapacityProposalRenderError(
            "coverage manifest must use a canonical repository path"
        )

    relative_text = relative.as_posix()
    if (
        relative_text != relative_text.strip()
        or any(character in relative_text for character in ("\x00", "\n", "\r"))
    ):
        raise CapacityProposalRenderError(
            "coverage manifest must use a canonical repository path"
        )

    current = root
    try:
        for part in relative.parts:
            current = current / part
            if os.path.islink(current):
                raise CapacityProposalRenderError(
                    "coverage manifest path must not traverse symlinks"
                )
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except CapacityProposalRenderError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise CapacityProposalRenderError(
            "coverage manifest must be a repository file"
        ) from exc

    return resolved, relative_text


def render_proposal(
    manifest_path: Path,
    candidate_max_sessions: object,
    *,
    expected_node_profile: object,
    expected_software_revision: object,
    repo_root: Path = ROOT,
) -> dict[str, Any]:
    resolved_manifest, manifest_name = _repository_manifest_path(
        manifest_path,
        repo_root=repo_root,
    )
    validator = _load_max_sessions_validator()
    try:
        summary = validator.validate_max_sessions(
            resolved_manifest,
            candidate_max_sessions,
            expected_node_profile=expected_node_profile,
            expected_software_revision=expected_software_revision,
            repo_root=repo_root,
        )
    except (validator.CapacityMaxSessionsError, OSError, UnicodeError) as exc:
        # Deployment identity and evidence may be operator/evidence controlled. Keep
        # renderer errors fixed rather than reflecting those strings into CI logs.
        raise CapacityProposalRenderError(
            "coverage manifest or deployment proposal is invalid"
        ) from exc

    return {
        "schema_version": 1,
        "coverage_manifest": manifest_name,
        "node_profile": summary["node_profile"],
        "software_revision": summary["software_revision"],
        "candidate_max_sessions": summary["candidate_max_sessions"],
        "measured_recommended_max_sessions": summary["recommended_max_sessions"],
        "headroom_sessions": summary["headroom_sessions"],
        "report_count": summary["report_count"],
        "safety_margin_percent": summary["safety_margin_percent"],
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "manifest",
        type=Path,
        help="repository-relative durable Node-capacity coverage manifest",
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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        proposal = render_proposal(
            args.manifest,
            args.max_sessions,
            expected_node_profile=args.node_profile,
            expected_software_revision=args.software_revision,
        )
    except CapacityProposalRenderError as exc:
        print(f"node capacity max_sessions proposal render failed: {exc}", file=sys.stderr)
        return 2

    json.dump(proposal, sys.stdout, indent=2, sort_keys=True, allow_nan=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
