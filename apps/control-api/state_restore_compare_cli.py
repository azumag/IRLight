"""Read-only comparison for a Control Plane state restore drill.

This command compares a protected reference snapshot with a separately restored
candidate.  It validates the same startup authority used by ``/readyz`` before
comparing bytes and never repairs, creates, or reconciles state.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import stat
from pathlib import Path
from typing import Any, Callable

from state_readiness import (
    StateReadinessError,
    _authority_specs,
    _open_regular_readonly,
    _reject_json_constant,
)
from node_internal import _validate_tokens
from state_safety import initialization_marker, load_json_authority


Validator = Callable[[dict[str, Any]], object]


def _validated_fingerprint(
    path: Path, validator: Validator, *, require_marker: bool = True
) -> tuple[bytes, tuple[int, int]]:
    """Validate and fingerprint one authority from the same read-only snapshot."""
    if require_marker:
        marker_fd = _open_regular_readonly(initialization_marker(path))
        os.close(marker_fd)

    fd = _open_regular_readonly(path)
    opened = os.fstat(fd)
    identity = (opened.st_dev, opened.st_ino)
    try:
        try:
            with os.fdopen(fd, "rb") as handle:
                raw = handle.read()
        except OSError as exc:
            raise StateReadinessError("required state cannot be read") from exc
        fd = -1
    finally:
        if fd >= 0:
            os.close(fd)

    try:
        text = raw.decode("utf-8")
        value = load_json_authority(
            io.StringIO(text), parse_constant=_reject_json_constant
        )
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise StateReadinessError("required state contains invalid JSON") from exc
    if not isinstance(value, dict):
        raise StateReadinessError("required state has invalid structure")
    try:
        validator(value)
    except Exception as exc:
        raise StateReadinessError("required state failed validation") from exc
    return hashlib.sha256(raw).digest(), identity


def _entry_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise StateReadinessError("required state entry cannot be inspected") from exc
    return True


def _legacy_fuse_fingerprint(
    node_state_dir: Path,
) -> tuple[bool, bool, bytes | None, tuple[int, int] | None]:
    """Return only presence flags and a validated digest for the legacy ledger."""
    path = node_state_dir / "bootstrap_tokens.json"
    marker = initialization_marker(path)
    file_present = _entry_exists(path)
    marker_present = _entry_exists(marker)

    if not file_present:
        if marker_present:
            raise StateReadinessError("legacy token fuse disappeared after initialization")
        return False, False, None, None

    if marker_present:
        marker_fd = _open_regular_readonly(marker)
        os.close(marker_fd)
    digest, identity = _validated_fingerprint(
        path, _validate_tokens, require_marker=False
    )
    return True, marker_present, digest, identity


def _directory_identity(path: Path) -> tuple[int, int]:
    """Open one snapshot root without following a raced final symlink."""
    try:
        before = path.lstat()
    except OSError as exc:
        raise StateReadinessError("snapshot root is unavailable") from exc
    if not stat.S_ISDIR(before.st_mode):
        raise StateReadinessError("snapshot root is unavailable")

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise StateReadinessError("snapshot root is unavailable") from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISDIR(opened.st_mode):
            raise StateReadinessError("snapshot root is unavailable")
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise StateReadinessError("snapshot root changed during inspection")
        return opened.st_dev, opened.st_ino
    finally:
        os.close(fd)


def _snapshot_root_reason(
    *,
    source_state_dir: Path,
    source_node_state_dir: Path,
    candidate_state_dir: Path,
    candidate_node_state_dir: Path,
) -> str | None:
    """Reject comparisons where any source and candidate root is the same directory."""
    try:
        source_ids = {
            _directory_identity(source_state_dir),
            _directory_identity(source_node_state_dir),
        }
        candidate_ids = {
            _directory_identity(candidate_state_dir),
            _directory_identity(candidate_node_state_dir),
        }
    except StateReadinessError:
        return "SNAPSHOT_ROOT_UNAVAILABLE"
    if source_ids & candidate_ids:
        return "SOURCE_CANDIDATE_NOT_DISTINCT"
    return None


def _unavailable(authority: str, side: str) -> dict[str, str]:
    return {
        "authority": authority,
        "status": "UNAVAILABLE",
        "reason_code": f"{side}_AUTHORITY_UNAVAILABLE",
    }


def compare_state_snapshots(
    *,
    source_state_dir: Path,
    candidate_state_dir: Path,
    source_node_state_dir: Path | None = None,
    candidate_node_state_dir: Path | None = None,
) -> dict[str, Any]:
    """Compare validated startup authority without exposing authority contents."""
    source_node_dir = source_node_state_dir or source_state_dir
    candidate_node_dir = candidate_node_state_dir or candidate_state_dir
    root_reason = _snapshot_root_reason(
        source_state_dir=source_state_dir,
        source_node_state_dir=source_node_dir,
        candidate_state_dir=candidate_state_dir,
        candidate_node_state_dir=candidate_node_dir,
    )
    if root_reason is not None:
        return {"status": "UNAVAILABLE", "reason_code": root_reason, "checks": []}

    source_specs = _authority_specs(
        state_dir=source_state_dir, node_state_dir=source_node_dir
    )
    candidate_specs = _authority_specs(
        state_dir=candidate_state_dir, node_state_dir=candidate_node_dir
    )

    checks: list[dict[str, str | None]] = []
    for source_spec, candidate_spec in zip(source_specs, candidate_specs, strict=True):
        authority, source_path, source_validator = source_spec
        candidate_authority, candidate_path, candidate_validator = candidate_spec
        if authority != candidate_authority:
            raise RuntimeError("authority specification mismatch")

        try:
            source_digest, source_identity = _validated_fingerprint(
                source_path, source_validator
            )
        except StateReadinessError:
            checks.append(_unavailable(authority, "SOURCE"))
            continue
        try:
            candidate_digest, candidate_identity = _validated_fingerprint(
                candidate_path, candidate_validator
            )
        except StateReadinessError:
            checks.append(_unavailable(authority, "CANDIDATE"))
            continue

        if source_identity == candidate_identity:
            checks.append(
                {
                    "authority": authority,
                    "status": "UNAVAILABLE",
                    "reason_code": "SOURCE_CANDIDATE_AUTHORITY_NOT_DISTINCT",
                }
            )
        elif source_digest == candidate_digest:
            checks.append({"authority": authority, "status": "MATCH", "reason_code": None})
        else:
            checks.append(
                {
                    "authority": authority,
                    "status": "MISMATCH",
                    "reason_code": "AUTHORITY_CONTENT_MISMATCH",
                }
            )

    legacy_authority = "legacy_bootstrap_tokens"
    try:
        source_legacy = _legacy_fuse_fingerprint(source_node_dir)
    except StateReadinessError:
        checks.append(_unavailable(legacy_authority, "SOURCE"))
    else:
        try:
            candidate_legacy = _legacy_fuse_fingerprint(candidate_node_dir)
        except StateReadinessError:
            checks.append(_unavailable(legacy_authority, "CANDIDATE"))
        else:
            source_file, source_marker, source_digest, source_identity = source_legacy
            candidate_file, candidate_marker, candidate_digest, candidate_identity = (
                candidate_legacy
            )
            if (
                source_file
                and candidate_file
                and source_identity == candidate_identity
            ):
                reason = "SOURCE_CANDIDATE_AUTHORITY_NOT_DISTINCT"
            elif source_file != candidate_file:
                reason = "LEGACY_FUSE_PRESENCE_MISMATCH"
            elif source_marker != candidate_marker:
                reason = "LEGACY_FUSE_MARKER_MISMATCH"
            elif source_digest != candidate_digest:
                reason = "AUTHORITY_CONTENT_MISMATCH"
            else:
                reason = None
            checks.append(
                {
                    "authority": legacy_authority,
                    "status": (
                        "MATCH"
                        if reason is None
                        else "UNAVAILABLE"
                        if reason == "SOURCE_CANDIDATE_AUTHORITY_NOT_DISTINCT"
                        else "MISMATCH"
                    ),
                    "reason_code": reason,
                }
            )

    if any(check["status"] == "UNAVAILABLE" for check in checks):
        status = "UNAVAILABLE"
    elif any(check["status"] == "MISMATCH" for check in checks):
        status = "MISMATCH"
    else:
        status = "MATCH"
    return {"status": status, "reason_code": None, "checks": checks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="state_restore_compare_cli")
    parser.add_argument("--source-state-dir", required=True)
    parser.add_argument("--candidate-state-dir", required=True)
    parser.add_argument("--source-node-state-dir")
    parser.add_argument("--candidate-node-state-dir")
    args = parser.parse_args(argv)

    payload = compare_state_snapshots(
        source_state_dir=Path(args.source_state_dir),
        candidate_state_dir=Path(args.candidate_state_dir),
        source_node_state_dir=Path(args.source_node_state_dir)
        if args.source_node_state_dir
        else None,
        candidate_node_state_dir=Path(args.candidate_node_state_dir)
        if args.candidate_node_state_dir
        else None,
    )
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return {"MATCH": 0, "MISMATCH": 2, "UNAVAILABLE": 3}[payload["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
