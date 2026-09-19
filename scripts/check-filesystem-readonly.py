#!/usr/bin/env python3
"""Read-only filesystem mount-state diagnostic.

The check uses statvfs(2) metadata only; it never creates a probe file. This is
intended for state/cache/output paths where an unexpected read-only remount can
make writes fail even while disk/inode capacity still looks healthy.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


PREFIX = "IRLIGHT_FILESYSTEM_MOUNT_HEALTH"


def _target_from_args(argv: list[str]) -> Path:
    if len(argv) > 2:
        raise ValueError("too many arguments")
    if len(argv) == 2:
        raw = argv[1]
    else:
        raw = os.getenv("IRLIGHT_FILESYSTEM_PATH") or os.getenv("STATE_DIR") or "/state"
    if not raw or "\x00" in raw:
        raise ValueError("invalid target")
    return Path(raw)


def evaluate(path: Path) -> tuple[int, str]:
    try:
        stats = os.statvfs(path)
    except (OSError, ValueError):
        return 3, f"{PREFIX} status=UNKNOWN reason=path_unavailable"

    read_only_flag = getattr(os, "ST_RDONLY", None)
    if not isinstance(read_only_flag, int) or read_only_flag <= 0:
        return 3, f"{PREFIX} status=UNKNOWN reason=readonly_flag_unavailable"

    if stats.f_flag & read_only_flag:
        return 2, f"{PREFIX} status=CRITICAL reason=filesystem_read_only read_only=true"
    return 0, f"{PREFIX} status=OK read_only=false"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv if argv is None else argv)
    try:
        path = _target_from_args(args)
    except ValueError:
        print(f"{PREFIX} status=UNKNOWN reason=invalid_target")
        return 3

    exit_code, line = evaluate(path)
    print(line)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
