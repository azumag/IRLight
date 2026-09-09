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
    _reject_json_constant,
)
from node_internal import _validate_tokens
from state_safety import initialization_marker, load_json_authority


Validator = Callable[[dict[str, Any]], object]


def _entry_name(path: Path) -> str:
    name = path.name
    if not name or name in {".", ".."} or Path(name).name != name:
        raise StateReadinessError("required state entry is unavailable")
    return name


def _open_regular_readonly_at(directory_fd: int, name: str) -> int:
    """Open one regular entry relative to an already verified root directory."""
    if not name or name in {".", ".."} or Path(name).name != name:
        raise StateReadinessError("required state entry is unavailable")
    try:
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except (OSError, NotImplementedError) as exc:
        raise StateReadinessError("required state entry is unavailable") from exc
    if not stat.S_ISREG(before.st_mode):
        raise StateReadinessError("required state entry is not a regular file")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(name, flags, dir_fd=directory_fd)
    except (OSError, NotImplementedError) as exc:
        raise StateReadinessError("required state entry cannot be opened") from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise StateReadinessError("required state entry is not a regular file")
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise StateReadinessError("required state entry changed during inspection")
        return fd
    except Exception:
        os.close(fd)
        raise


def _assert_regular_entry_unchanged_at(directory_fd: int, name: str, fd: int) -> None:
    """Fail closed if a pinned snapshot entry was replaced during validation."""
    try:
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        opened = os.fstat(fd)
    except (OSError, NotImplementedError) as exc:
        raise StateReadinessError("required state entry changed during inspection") from exc
    if not stat.S_ISREG(current.st_mode):
        raise StateReadinessError("required state entry changed during inspection")
    if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
        raise StateReadinessError("required state entry changed during inspection")


def _validated_fingerprint_at(
    directory_fd: int, path: Path, validator: Validator, *, require_marker: bool = True
) -> tuple[bytes, tuple[int, int]]:
    """Validate/fingerprint one authority without re-resolving its snapshot root."""
    name = _entry_name(path)
    marker_name: str | None = None
    marker_fd: int | None = None
    fd: int | None = None
    try:
        if require_marker:
            marker_name = initialization_marker(Path(name)).name
            marker_fd = _open_regular_readonly_at(directory_fd, marker_name)

        fd = _open_regular_readonly_at(directory_fd, name)
        opened = os.fstat(fd)
        identity = (opened.st_dev, opened.st_ino)
        try:
            handle = os.fdopen(os.dup(fd), "rb")
        except OSError as exc:
            raise StateReadinessError("required state cannot be read") from exc
        with handle:
            try:
                raw = handle.read()
            except OSError as exc:
                raise StateReadinessError("required state cannot be read") from exc

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

        # The protected reference can still be on a writable filesystem during
        # a drill, and the candidate may have a restore process or operator
        # touching it by mistake.  Do not compare a stale inode snapshot after
        # an atomic replace; require the pathname to still name the entry that
        # was actually read and validated.  Keep the initialization marker
        # pinned across validation for the same reason.
        _assert_regular_entry_unchanged_at(directory_fd, name, fd)
        if marker_fd is not None and marker_name is not None:
            _assert_regular_entry_unchanged_at(directory_fd, marker_name, marker_fd)
        return hashlib.sha256(raw).digest(), identity
    finally:
        if fd is not None:
            os.close(fd)
        if marker_fd is not None:
            os.close(marker_fd)


def _entry_exists_at(directory_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except (OSError, NotImplementedError) as exc:
        raise StateReadinessError("required state entry cannot be inspected") from exc
    return True


def _legacy_fuse_fingerprint_at(
    node_directory_fd: int,
) -> tuple[bool, bool, bytes | None, tuple[int, int] | None]:
    """Return only presence flags and a validated digest for the legacy ledger."""
    name = "bootstrap_tokens.json"
    marker_name = initialization_marker(Path(name)).name
    file_present = _entry_exists_at(node_directory_fd, name)
    marker_present = _entry_exists_at(node_directory_fd, marker_name)

    if not file_present:
        if marker_present:
            raise StateReadinessError("legacy token fuse disappeared after initialization")
        return False, False, None, None

    digest, identity = _validated_fingerprint_at(
        node_directory_fd,
        Path(name),
        _validate_tokens,
        require_marker=marker_present,
    )
    # A legacy file predates mandatory markers, so absence remains compatible.
    # But if a marker appears while this inspection is in flight, the presence
    # bit used for source/candidate comparison is stale and must not be trusted.
    if _entry_exists_at(node_directory_fd, marker_name) != marker_present:
        raise StateReadinessError("required state entry changed during inspection")
    return True, marker_present, digest, identity


def _open_snapshot_root(path: Path) -> tuple[int, tuple[int, int]]:
    """Open and retain one snapshot root without following final symlinks."""
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise StateReadinessError("snapshot root cannot be inspected safely")

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if not path.name:
        try:
            before = path.lstat()
            fd = os.open(path, flags)
        except (OSError, NotImplementedError) as exc:
            raise StateReadinessError("snapshot root is unavailable") from exc
        try:
            opened = os.fstat(fd)
            if (
                not stat.S_ISDIR(before.st_mode)
                or not stat.S_ISDIR(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            ):
                raise StateReadinessError("snapshot root is unavailable")
            return fd, (opened.st_dev, opened.st_ino)
        except Exception:
            os.close(fd)
            raise

    parent = path.parent
    try:
        parent_before = parent.lstat()
    except OSError as exc:
        raise StateReadinessError("snapshot root is unavailable") from exc
    if not stat.S_ISDIR(parent_before.st_mode):
        raise StateReadinessError("snapshot root is unavailable")

    try:
        parent_fd = os.open(parent, flags)
    except (OSError, NotImplementedError) as exc:
        raise StateReadinessError("snapshot root is unavailable") from exc
    try:
        parent_opened = os.fstat(parent_fd)
        if (
            not stat.S_ISDIR(parent_opened.st_mode)
            or (parent_opened.st_dev, parent_opened.st_ino)
            != (parent_before.st_dev, parent_before.st_ino)
        ):
            raise StateReadinessError("snapshot root is unavailable")
        try:
            before = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            fd = os.open(path.name, flags, dir_fd=parent_fd)
        except (OSError, NotImplementedError) as exc:
            raise StateReadinessError("snapshot root is unavailable") from exc
        try:
            opened = os.fstat(fd)
            if (
                not stat.S_ISDIR(before.st_mode)
                or not stat.S_ISDIR(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            ):
                raise StateReadinessError("snapshot root is unavailable")
            return fd, (opened.st_dev, opened.st_ino)
        except Exception:
            os.close(fd)
            raise
    finally:
        os.close(parent_fd)


def _root_fd_for_authority(
    path: Path, *, state_dir: Path, state_fd: int, node_state_dir: Path, node_fd: int
) -> int:
    if node_state_dir != state_dir and path.parent == node_state_dir:
        return node_fd
    return state_fd


def _snapshot_roots_still_current(
    roots: list[tuple[Path, tuple[int, int]]],
) -> bool:
    """Fail closed if a pathname no longer resolves to the root opened at start."""
    for path, expected_identity in roots:
        try:
            fd, current_identity = _open_snapshot_root(path)
        except StateReadinessError:
            return False
        try:
            if current_identity != expected_identity:
                return False
        finally:
            os.close(fd)
    return True


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

    root_fds: list[int] = []
    root_expectations: list[tuple[Path, tuple[int, int]]] = []
    try:
        try:
            source_state_fd, source_state_id = _open_snapshot_root(source_state_dir)
            root_fds.append(source_state_fd)
            root_expectations.append((source_state_dir, source_state_id))
            if source_node_dir == source_state_dir:
                source_node_fd, source_node_id = source_state_fd, source_state_id
            else:
                source_node_fd, source_node_id = _open_snapshot_root(source_node_dir)
                root_fds.append(source_node_fd)
                root_expectations.append((source_node_dir, source_node_id))

            candidate_state_fd, candidate_state_id = _open_snapshot_root(candidate_state_dir)
            root_fds.append(candidate_state_fd)
            root_expectations.append((candidate_state_dir, candidate_state_id))
            if candidate_node_dir == candidate_state_dir:
                candidate_node_fd, candidate_node_id = candidate_state_fd, candidate_state_id
            else:
                candidate_node_fd, candidate_node_id = _open_snapshot_root(
                    candidate_node_dir
                )
                root_fds.append(candidate_node_fd)
                root_expectations.append((candidate_node_dir, candidate_node_id))
        except StateReadinessError:
            return {
                "status": "UNAVAILABLE",
                "reason_code": "SNAPSHOT_ROOT_UNAVAILABLE",
                "checks": [],
            }

        source_ids = {source_state_id, source_node_id}
        candidate_ids = {candidate_state_id, candidate_node_id}
        if source_ids & candidate_ids:
            return {
                "status": "UNAVAILABLE",
                "reason_code": "SOURCE_CANDIDATE_NOT_DISTINCT",
                "checks": [],
            }

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

            source_root_fd = _root_fd_for_authority(
                source_path,
                state_dir=source_state_dir,
                state_fd=source_state_fd,
                node_state_dir=source_node_dir,
                node_fd=source_node_fd,
            )
            candidate_root_fd = _root_fd_for_authority(
                candidate_path,
                state_dir=candidate_state_dir,
                state_fd=candidate_state_fd,
                node_state_dir=candidate_node_dir,
                node_fd=candidate_node_fd,
            )
            try:
                source_digest, source_identity = _validated_fingerprint_at(
                    source_root_fd, source_path, source_validator
                )
            except StateReadinessError:
                checks.append(_unavailable(authority, "SOURCE"))
                continue
            try:
                candidate_digest, candidate_identity = _validated_fingerprint_at(
                    candidate_root_fd, candidate_path, candidate_validator
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
                checks.append(
                    {"authority": authority, "status": "MATCH", "reason_code": None}
                )
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
            source_legacy = _legacy_fuse_fingerprint_at(source_node_fd)
        except StateReadinessError:
            checks.append(_unavailable(legacy_authority, "SOURCE"))
        else:
            try:
                candidate_legacy = _legacy_fuse_fingerprint_at(candidate_node_fd)
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

        if not _snapshot_roots_still_current(root_expectations):
            return {
                "status": "UNAVAILABLE",
                "reason_code": "SNAPSHOT_ROOT_UNAVAILABLE",
                "checks": [],
            }

        if any(check["status"] == "UNAVAILABLE" for check in checks):
            status = "UNAVAILABLE"
        elif any(check["status"] == "MISMATCH" for check in checks):
            status = "MISMATCH"
        else:
            status = "MATCH"
        return {"status": status, "reason_code": None, "checks": checks}
    finally:
        for fd in reversed(root_fds):
            os.close(fd)


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
