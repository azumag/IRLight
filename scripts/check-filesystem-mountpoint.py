#!/usr/bin/env python3
"""Read-only check that an expected path is an exact mount point.

The checker reads Linux /proc/self/mountinfo and never probes writeability or
changes mount state. It is intended to catch a missing state/cache/output mount
that would otherwise fall through to an underlying filesystem.
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


def _decode_mountinfo_path(raw: str) -> str:
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


def _mountpoints(text: str) -> set[str]:
    if not text:
        raise ValueError("empty mountinfo")

    mountpoints: set[str] = set()
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
        mountpoints.add(os.path.normpath(mountpoint))
    return mountpoints


def evaluate(path: Path, mountinfo_path: Path) -> tuple[int, str]:
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
        mountpoints = _mountpoints(text)
    except (OSError, UnicodeError, ValueError):
        return 3, f"{PREFIX} status=UNKNOWN reason=mountinfo_unavailable"

    target = os.path.normpath(str(path))
    if target not in mountpoints:
        return 2, f"{PREFIX} status=CRITICAL reason=mountpoint_missing"
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

    exit_code, line = evaluate(target, Path(raw_mountinfo_path))
    print(line)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
