"""Read-only inspection for durable JSON authority files.

The normal stores intentionally create lock files, initialization markers, and
empty authority state on some bootstrap paths. Readiness and recovery tooling
must not call those paths just to answer whether existing state is trustworthy.
This module inspects an already persisted authority snapshot without creating,
repairing, truncating, or locking repository state.
"""

from __future__ import annotations

import argparse
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from state_safety import initialization_marker, load_json_authority


READY = "READY"
AUTHORITY_UNINITIALIZED = "AUTHORITY_UNINITIALIZED"
AUTHORITY_MARKER_MISSING = "AUTHORITY_MARKER_MISSING"
AUTHORITY_MARKER_INVALID = "AUTHORITY_MARKER_INVALID"
AUTHORITY_MISSING_AFTER_INIT = "AUTHORITY_MISSING_AFTER_INIT"
AUTHORITY_FILE_INVALID = "AUTHORITY_FILE_INVALID"
AUTHORITY_UNREADABLE = "AUTHORITY_UNREADABLE"
AUTHORITY_JSON_INVALID = "AUTHORITY_JSON_INVALID"
AUTHORITY_SCHEMA_INVALID = "AUTHORITY_SCHEMA_INVALID"
AUTHORITY_CHANGED_DURING_READ = "AUTHORITY_CHANGED_DURING_READ"


@dataclass(frozen=True)
class AuthorityInspection:
    ready: bool
    reason: str

    def public_dict(self) -> dict[str, object]:
        """Return a path- and content-free result safe for readiness output."""
        return {"ready": self.ready, "reason": self.reason}


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _lstat_optional(path: Path) -> tuple[os.stat_result | None, str | None]:
    try:
        return path.lstat(), None
    except FileNotFoundError:
        return None, None
    except OSError:
        return None, AUTHORITY_UNREADABLE


def inspect_json_authority(
    path: str | os.PathLike[str],
    *,
    required_keys: Iterable[str] = (),
) -> AuthorityInspection:
    """Inspect one initialized JSON authority without mutating its directory.

    A healthy snapshot requires both the durable initialization marker and the
    authority file to be regular files. The authority is opened with
    ``O_NOFOLLOW`` when available, decoded with the same strict JSON loader as
    the writers, checked for an object root and caller-supplied top-level keys,
    then re-statted so an atomic replacement during inspection is reported as
    unavailable rather than trusted as a stable snapshot.

    The function deliberately does not create or acquire the store lock. It is
    a non-blocking physical/syntax/schema-boundary check, not a substitute for
    store-specific semantic validation. Callers should fail closed on every
    result except ``READY``.
    """

    authority_path = Path(path)
    marker_path = initialization_marker(authority_path)

    marker_before, marker_error = _lstat_optional(marker_path)
    if marker_error is not None:
        return AuthorityInspection(False, marker_error)
    authority_before, authority_error = _lstat_optional(authority_path)
    if authority_error is not None:
        return AuthorityInspection(False, authority_error)

    if marker_before is None:
        if authority_before is None:
            return AuthorityInspection(False, AUTHORITY_UNINITIALIZED)
        return AuthorityInspection(False, AUTHORITY_MARKER_MISSING)
    if not stat.S_ISREG(marker_before.st_mode):
        return AuthorityInspection(False, AUTHORITY_MARKER_INVALID)

    if authority_before is None:
        return AuthorityInspection(False, AUTHORITY_MISSING_AFTER_INIT)
    if not stat.S_ISREG(authority_before.st_mode):
        return AuthorityInspection(False, AUTHORITY_FILE_INVALID)

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(authority_path, flags)
    except (OSError, ValueError):
        return AuthorityInspection(False, AUTHORITY_UNREADABLE)

    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            return AuthorityInspection(False, AUTHORITY_FILE_INVALID)
        if not _same_identity(authority_before, opened):
            return AuthorityInspection(False, AUTHORITY_CHANGED_DURING_READ)

        try:
            with os.fdopen(os.dup(fd), "r", encoding="utf-8", errors="strict") as handle:
                payload = load_json_authority(handle)
        except (OSError, UnicodeError, ValueError):
            return AuthorityInspection(False, AUTHORITY_JSON_INVALID)

        if not isinstance(payload, dict):
            return AuthorityInspection(False, AUTHORITY_SCHEMA_INVALID)
        for key in required_keys:
            if not isinstance(key, str) or not key or key not in payload:
                return AuthorityInspection(False, AUTHORITY_SCHEMA_INVALID)

        marker_after, marker_error = _lstat_optional(marker_path)
        authority_after, authority_error = _lstat_optional(authority_path)
        if marker_error is not None or authority_error is not None:
            return AuthorityInspection(False, AUTHORITY_UNREADABLE)
        if marker_after is None or authority_after is None:
            return AuthorityInspection(False, AUTHORITY_CHANGED_DURING_READ)
        if not stat.S_ISREG(marker_after.st_mode) or not stat.S_ISREG(authority_after.st_mode):
            return AuthorityInspection(False, AUTHORITY_CHANGED_DURING_READ)
        if not _same_identity(marker_before, marker_after):
            return AuthorityInspection(False, AUTHORITY_CHANGED_DURING_READ)
        if not _same_identity(opened, authority_after):
            return AuthorityInspection(False, AUTHORITY_CHANGED_DURING_READ)
    finally:
        os.close(fd)

    return AuthorityInspection(True, READY)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect an existing IRLight JSON authority without mutating it."
    )
    parser.add_argument("path", help="authority JSON path")
    parser.add_argument(
        "--require-key",
        action="append",
        default=[],
        dest="required_keys",
        help="required top-level object key (repeatable)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    result = inspect_json_authority(args.path, required_keys=args.required_keys)
    print(f"ready={'yes' if result.ready else 'no'} reason={result.reason}")
    return 0 if result.ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
