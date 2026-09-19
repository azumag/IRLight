#!/usr/bin/env python3
"""Read-only check that an expected path is an exact mount point.

The checker reads Linux /proc/self/mountinfo and never probes writeability or
changes mount state. It is intended to catch a missing state/cache/output mount
that would otherwise fall through to an underlying filesystem. Operators may
optionally pin the current mount namespace entry to an expected source and/or
mount root without changing the default presence-only contract.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path


# The operational script may be invoked from any working directory. Add the
# checkout root explicitly so the shared parser remains available without
# relying on cwd/PYTHONPATH side effects.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mountinfo_identity import (  # noqa: E402
    MountInfoError,
    decode_mountinfo_field,
    identity_matches,
    normalize_expected_identity,
    parse_mountinfo_entries,
    select_exact_mounts,
)


PREFIX = "IRLIGHT_FILESYSTEM_MOUNTPOINT_HEALTH"
_DEFAULT_MOUNTINFO = "/proc/self/mountinfo"


def _target_from_args(argv: list[str]) -> Path:
    if len(argv) > 2:
        raise ValueError("too many arguments")
    if len(argv) == 2:
        raw = argv[1]
    else:
        raw = (
            os.getenv("IRLIGHT_EXPECTED_MOUNTPOINT_PATH")
            or os.getenv("STATE_DIR")
            or "/state"
        )
    if not raw or "\x00" in raw:
        raise ValueError("invalid target")
    target = Path(raw)
    if not target.is_absolute():
        raise ValueError("target must be absolute")
    return Path(os.path.normpath(str(target)))


def _decode_mountinfo_field(raw: str) -> str:
    """Compatibility wrapper for existing focused tests/callers."""
    try:
        return decode_mountinfo_field(raw)
    except MountInfoError as exc:
        raise ValueError(str(exc)) from exc


def _decode_mountinfo_path(raw: str) -> str:
    return _decode_mountinfo_field(raw)


def _mount_entries(text: str) -> list[tuple[str, str, str]]:
    """Compatibility wrapper over the shared parser's immutable entries."""
    try:
        entries = parse_mountinfo_entries(text)
    except MountInfoError as exc:
        raise ValueError(str(exc)) from exc
    return [(entry.mountpoint, entry.raw_root, entry.raw_source) for entry in entries]


def _mountpoints(text: str) -> set[str]:
    return {mountpoint for mountpoint, _root, _source in _mount_entries(text)}


def _expected_identity_from_env() -> tuple[str | None, str | None]:
    source_present = "IRLIGHT_EXPECTED_MOUNT_SOURCE" in os.environ
    root_present = "IRLIGHT_EXPECTED_MOUNT_ROOT" in os.environ
    if not source_present and not root_present:
        return None, None

    source = os.environ.get("IRLIGHT_EXPECTED_MOUNT_SOURCE") if source_present else None
    root = os.environ.get("IRLIGHT_EXPECTED_MOUNT_ROOT") if root_present else None
    try:
        return normalize_expected_identity(source, root)
    except MountInfoError as exc:
        raise ValueError(str(exc)) from exc


def evaluate(
    path: Path,
    mountinfo_path: Path,
    *,
    expected_source: str | None = None,
    expected_root: str | None = None,
) -> tuple[int, str]:
    try:
        target_stat = os.lstat(path)
    except FileNotFoundError:
        return 2, f"{PREFIX} status=CRITICAL reason=mountpoint_missing"
    except (OSError, ValueError):
        return 3, f"{PREFIX} status=UNKNOWN reason=target_unavailable"

    if stat.S_ISLNK(target_stat.st_mode):
        return 3, f"{PREFIX} status=UNKNOWN reason=target_symlink"

    try:
        # Linux pathnames are byte strings and mountinfo may contain bytes that
        # are not valid UTF-8. surrogateescape preserves them losslessly while
        # still letting us compare normal configured paths without rejecting an
        # otherwise unrelated mount record.
        text = mountinfo_path.read_text(encoding="utf-8", errors="surrogateescape")
        entries = parse_mountinfo_entries(text)
    except (OSError, UnicodeError, MountInfoError):
        return 3, f"{PREFIX} status=UNKNOWN reason=mountinfo_unavailable"

    matches = select_exact_mounts(entries, path)
    if not matches:
        return 2, f"{PREFIX} status=CRITICAL reason=mountpoint_missing"

    identity_enabled = expected_source is not None or expected_root is not None
    if identity_enabled and len(matches) != 1:
        return 3, f"{PREFIX} status=UNKNOWN reason=mountpoint_ambiguous"

    if identity_enabled:
        try:
            matches_identity = identity_matches(
                matches[0],
                expected_source=expected_source,
                expected_root=expected_root,
            )
        except MountInfoError:
            return 3, f"{PREFIX} status=UNKNOWN reason=mountinfo_unavailable"
        if not matches_identity:
            return 2, f"{PREFIX} status=CRITICAL reason=mount_identity_mismatch"

    return 0, f"{PREFIX} status=OK mounted=true"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv if argv is None else argv)
    try:
        target = _target_from_args(args)
    except ValueError:
        print(f"{PREFIX} status=UNKNOWN reason=invalid_target")
        return 3

    raw_mountinfo_path = os.getenv("IRLIGHT_MOUNTINFO_PATH") or _DEFAULT_MOUNTINFO
    if not raw_mountinfo_path or "\x00" in raw_mountinfo_path:
        print(f"{PREFIX} status=UNKNOWN reason=invalid_mountinfo_path")
        return 3

    try:
        expected_source, expected_root = _expected_identity_from_env()
    except ValueError:
        print(f"{PREFIX} status=UNKNOWN reason=invalid_expected_mount_identity")
        return 3

    exit_code, line = evaluate(
        target,
        Path(raw_mountinfo_path),
        expected_source=expected_source,
        expected_root=expected_root,
    )
    print(line)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
