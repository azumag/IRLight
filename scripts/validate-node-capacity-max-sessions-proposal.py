#!/usr/bin/env python3
"""Validate a persisted Node-capacity max_sessions proposal against its evidence.

This is a read-only review guardrail. It loads a schema-v1 proposal from a
canonical repository path, recomputes that proposal from the referenced
coverage manifest with the canonical renderer, binds it to an explicitly
supplied Node profile and software revision, and requires byte-for-byte
canonical JSON equivalence of the decoded values. It never edits scheduler
inventory or Node configuration, executes load, contacts a provider, or
chooses capacity policy.
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

ROOT = Path(__file__).resolve().parents[1]
PROPOSAL_RENDERER = Path(__file__).with_name(
    "render-node-capacity-max-sessions-proposal.py"
)
MAX_PROPOSAL_BYTES = 128 * 1024
PROPOSAL_FIELDS = {
    "schema_version",
    "coverage_manifest",
    "node_profile",
    "software_revision",
    "candidate_max_sessions",
    "measured_recommended_max_sessions",
    "headroom_sessions",
    "report_count",
    "safety_margin_percent",
}


class CapacityProposalValidationError(ValueError):
    """Raised when a persisted max_sessions proposal is unsafe or stale."""


def _load_renderer() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "irlight_node_capacity_persisted_proposal_renderer",
        PROPOSAL_RENDERER,
    )
    if spec is None or spec.loader is None:
        raise CapacityProposalValidationError("proposal renderer could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # pragma: no cover - repository packaging failure
        raise CapacityProposalValidationError(
            "proposal renderer could not be loaded"
        ) from exc
    return module


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CapacityProposalValidationError("proposal contains a duplicate JSON key")
        result[key] = value
    return result


def _reject_nonfinite_constant(_value: str) -> None:
    raise CapacityProposalValidationError("proposal contains non-standard JSON")


def _identity(snapshot: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        snapshot.st_dev,
        snapshot.st_ino,
        snapshot.st_size,
        snapshot.st_mtime_ns,
        snapshot.st_ctime_ns,
    )


def _repository_file(path: Path, *, repo_root: Path) -> Path:
    """Resolve one symlink-free regular repository file without trusting its contents."""

    try:
        root = repo_root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CapacityProposalValidationError("repository root could not be resolved") from exc

    try:
        if path.is_absolute():
            candidate = path
            relative = candidate.relative_to(root)
        else:
            relative = path
            candidate = root / relative
    except ValueError as exc:
        raise CapacityProposalValidationError("proposal must be a repository file") from exc

    if (
        not relative.parts
        or relative.is_absolute()
        or any(part in ("", ".", "..") for part in relative.parts)
    ):
        raise CapacityProposalValidationError("proposal must use a canonical repository path")
    relative_text = relative.as_posix()
    if (
        relative_text != relative_text.strip()
        or any(character in relative_text for character in ("\x00", "\n", "\r"))
    ):
        raise CapacityProposalValidationError("proposal must use a canonical repository path")

    current = root
    try:
        for part in relative.parts:
            current = current / part
            if os.path.islink(current):
                raise CapacityProposalValidationError("proposal path must not traverse symlinks")
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
        snapshot = os.lstat(candidate)
    except CapacityProposalValidationError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise CapacityProposalValidationError("proposal must be a repository file") from exc
    if not stat.S_ISREG(snapshot.st_mode):
        raise CapacityProposalValidationError("proposal must be a regular file")
    return resolved


def _read_stable_bytes(path: Path) -> bytes:
    try:
        before = os.lstat(path)
    except OSError as exc:
        raise CapacityProposalValidationError("proposal could not be inspected") from exc
    if not stat.S_ISREG(before.st_mode):
        raise CapacityProposalValidationError("proposal must be a regular file")
    if before.st_size > MAX_PROPOSAL_BYTES:
        raise CapacityProposalValidationError("proposal exceeds size limit")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise CapacityProposalValidationError("proposal could not be opened") from exc

    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise CapacityProposalValidationError("proposal must be a regular file")
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise CapacityProposalValidationError("proposal changed while opening")
        if opened.st_size > MAX_PROPOSAL_BYTES:
            raise CapacityProposalValidationError("proposal exceeds size limit")

        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(MAX_PROPOSAL_BYTES + 1)
        if len(raw) > MAX_PROPOSAL_BYTES:
            raise CapacityProposalValidationError("proposal exceeds size limit")

        after_read = os.fstat(fd)
        try:
            after_path = os.lstat(path)
        except OSError as exc:
            raise CapacityProposalValidationError("proposal changed while reading") from exc
        if not stat.S_ISREG(after_path.st_mode):
            raise CapacityProposalValidationError("proposal changed while reading")
        if _identity(opened) != _identity(after_read) or _identity(opened) != _identity(after_path):
            raise CapacityProposalValidationError("proposal changed while reading")
        return raw
    finally:
        os.close(fd)


def _load_proposal(path: Path) -> dict[str, Any]:
    try:
        text = _read_stable_bytes(path).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CapacityProposalValidationError("proposal must be valid UTF-8") from exc
    try:
        payload = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite_constant,
        )
    except CapacityProposalValidationError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise CapacityProposalValidationError("proposal is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise CapacityProposalValidationError("proposal root must be an object")
    return payload


def _positive_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CapacityProposalValidationError("proposal integer field is invalid")
    return value


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def validate_proposal_file(
    proposal_path: Path,
    *,
    expected_node_profile: object,
    expected_software_revision: object,
    repo_root: Path = ROOT,
) -> dict[str, Any]:
    resolved_proposal = _repository_file(proposal_path, repo_root=repo_root)
    payload = _load_proposal(resolved_proposal)
    if set(payload) != PROPOSAL_FIELDS:
        raise CapacityProposalValidationError("proposal has an unexpected top-level shape")
    if (
        isinstance(payload["schema_version"], bool)
        or not isinstance(payload["schema_version"], int)
        or payload["schema_version"] != 1
    ):
        raise CapacityProposalValidationError("proposal schema_version is unsupported")

    coverage_name = payload["coverage_manifest"]
    if not isinstance(coverage_name, str) or not coverage_name:
        raise CapacityProposalValidationError("proposal coverage reference is invalid")
    candidate = _positive_int(payload["candidate_max_sessions"])

    renderer = _load_renderer()
    try:
        expected = renderer.render_proposal(
            repo_root / coverage_name,
            candidate,
            expected_node_profile=expected_node_profile,
            expected_software_revision=expected_software_revision,
            repo_root=repo_root,
        )
    except (renderer.CapacityProposalRenderError, OSError, UnicodeError) as exc:
        raise CapacityProposalValidationError(
            "proposal evidence or deployment identity is invalid"
        ) from exc

    try:
        matches = _canonical_json(payload) == _canonical_json(expected)
    except (TypeError, ValueError) as exc:
        raise CapacityProposalValidationError("proposal contains an invalid value") from exc
    if not matches:
        raise CapacityProposalValidationError(
            "proposal does not match current canonical measured evidence"
        )
    return expected


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
        proposal = validate_proposal_file(
            args.proposal,
            expected_node_profile=args.node_profile,
            expected_software_revision=args.software_revision,
        )
    except (CapacityProposalValidationError, OSError) as exc:
        print(f"node capacity max_sessions proposal invalid: {exc}", file=sys.stderr)
        return 2

    print(
        "node capacity max_sessions proposal valid: "
        f"candidate={proposal['candidate_max_sessions']} "
        f"measured_recommendation={proposal['measured_recommended_max_sessions']} "
        f"headroom={proposal['headroom_sessions']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
