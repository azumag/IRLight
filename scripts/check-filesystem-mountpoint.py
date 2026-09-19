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
import re
import stat
import sys
from pathlib import Path


PREFIX = "IRLIGHT_FILESYSTEM_MOUNTPOINT_HEALTH"
_DEFAULT_MOUNTINFO = "/proc/self/mountinfo"
_ESCAPE_RE = re.compile(r"\\([0-7]{3})")
_ALLOWED_ESCAPES = {"011", "012", "040", "134"}


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
    cursor = 0
    decoded: list[str] = []
    for match in _ESCAPE_RE.finditer(raw):
        literal = raw[cursor : match.start()]
        if "\\" in literal or match.group(1) not in _ALLOWED_ESCAPES:
            raise ValueError("invalid mountinfo escape")
        decoded.append(literal)
        decoded.append(chr(int(match.group(1), 8)))
        cursor = match.end()
    tail = raw[cursor:]
    if "\\" in tail:
        raise ValueError("invalid mountinfo escape")
    decoded.append(tail)
    return "".join(decoded)


def _decode_mountinfo_path(raw: str) -> str:
    return _decode_mountinfo_field(raw)


def _mount_entries(text: str) -> list[tuple[str, str, str]]:
    if not text:
        raise ValueError("empty mountinfo")

    entries: list[tuple[str, str, str]] = []
    for raw_line in text.splitlines():
        if not raw_line:
            raise ValueError("empty mountinfo record")
        fields = raw_line.split()
        if len(fields) < 10:
            raise ValueError("short mountinfo record")
        try:
            separator = fields.index("-", 6)
        except ValueError as exc:
            raise ValueError("missing mountinfo separator") from exc
        if separator + 3 >= len(fields):
            raise ValueError("short mountinfo suffix")

        mountpoint = _decode_mountinfo_path(fields[4])
        if not mountpoint.startswith("/"):
            raise ValueError("relative mount point")
        # Preserve the legacy presence-only contract: root/source fields are
        # opaque unless identity verification is explicitly enabled, so an
        # unrelated record cannot make a presence check fail merely because
        # one of those additional fields is unusual.
        entries.append(
            (
                os.path.normpath(mountpoint),
                fields[3],
                fields[separator + 2],
            )
        )
    return entries


def _mountpoints(text: str) -> set[str]:
    return {mountpoint for mountpoint, _root, _source in _mount_entries(text)}


def _expected_identity_from_env() -> tuple[str | None, str | None]:
    expected_source: str | None = None
    expected_root: str | None = None

    if "IRLIGHT_EXPECTED_MOUNT_SOURCE" in os.environ:
        raw_source = os.environ["IRLIGHT_EXPECTED_MOUNT_SOURCE"]
        if not raw_source or "\x00" in raw_source:
            raise ValueError("invalid expected source")
        expected_source = raw_source

    if "IRLIGHT_EXPECTED_MOUNT_ROOT" in os.environ:
        raw_root = os.environ["IRLIGHT_EXPECTED_MOUNT_ROOT"]
        if not raw_root or "\x00" in raw_root:
            raise ValueError("invalid expected root")
        root = Path(raw_root)
        if not root.is_absolute():
            raise ValueError("expected root must be absolute")
        expected_root = os.path.normpath(str(root))

    return expected_source, expected_root


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
        entries = _mount_entries(text)
    except (OSError, UnicodeError, ValueError):
        return 3, f"{PREFIX} status=UNKNOWN reason=mountinfo_unavailable"

    target = os.path.normpath(str(path))
    matches = [entry for entry in entries if entry[0] == target]
    if not matches:
        return 2, f"{PREFIX} status=CRITICAL reason=mountpoint_missing"

    identity_enabled = expected_source is not None or expected_root is not None
    if identity_enabled and len(matches) != 1:
        return 3, f"{PREFIX} status=UNKNOWN reason=mountpoint_ambiguous"

    if identity_enabled:
        _mountpoint, raw_root, raw_source = matches[0]
        try:
            actual_root = _decode_mountinfo_path(raw_root)
            actual_source = _decode_mountinfo_field(raw_source)
            if not actual_root.startswith("/"):
                raise ValueError("relative mount root")
            actual_root = os.path.normpath(actual_root)
        except ValueError:
            return 3, f"{PREFIX} status=UNKNOWN reason=mountinfo_unavailable"
        if expected_source is not None and actual_source != expected_source:
            return 2, f"{PREFIX} status=CRITICAL reason=mount_identity_mismatch"
        if expected_root is not None and actual_root != expected_root:
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
