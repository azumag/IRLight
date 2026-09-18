#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from typing import Sequence


PREFIX = "IRLIGHT_FILESYSTEM_CAPACITY"


def _emit(
    status: str,
    reason: str,
    *,
    available_bytes: int | None = None,
    available_inodes: int | str | None = None,
) -> int:
    fields = [PREFIX, f"status={status}", f"reason={reason}"]
    if available_bytes is not None:
        fields.append(f"available_bytes={available_bytes}")
    if available_inodes is not None:
        fields.append(f"available_inodes={available_inodes}")
    print(" ".join(fields))
    return {"OK": 0, "CRITICAL": 2, "UNKNOWN": 3}[status]


def _valid_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def evaluate_filesystem(path: str) -> int:
    if not path:
        return _emit("UNKNOWN", "path_unavailable")

    try:
        stats = os.statvfs(path)
    except (OSError, ValueError):
        return _emit("UNKNOWN", "path_unavailable")

    fields = (
        stats.f_bsize,
        stats.f_frsize,
        stats.f_blocks,
        stats.f_bfree,
        stats.f_bavail,
        stats.f_files,
        stats.f_ffree,
        stats.f_favail,
    )
    if any(not _valid_nonnegative_int(value) for value in fields):
        return _emit("UNKNOWN", "invalid_statvfs")

    if stats.f_blocks == 0:
        return _emit("UNKNOWN", "block_capacity_unavailable")
    if stats.f_bavail > stats.f_blocks or stats.f_bfree > stats.f_blocks:
        return _emit("UNKNOWN", "invalid_statvfs")

    block_size = stats.f_frsize or stats.f_bsize
    if block_size <= 0:
        return _emit("UNKNOWN", "block_capacity_unavailable")

    inode_supported = stats.f_files > 0
    if inode_supported and (stats.f_favail > stats.f_files or stats.f_ffree > stats.f_files):
        return _emit("UNKNOWN", "invalid_statvfs")

    available_bytes = stats.f_bavail * block_size
    available_inodes: int | str = stats.f_favail if inode_supported else "unsupported"

    blocks_exhausted = stats.f_bavail == 0
    inodes_exhausted = inode_supported and stats.f_favail == 0

    if blocks_exhausted and inodes_exhausted:
        return _emit(
            "CRITICAL",
            "blocks_and_inodes_exhausted",
            available_bytes=available_bytes,
            available_inodes=available_inodes,
        )
    if blocks_exhausted:
        return _emit(
            "CRITICAL",
            "blocks_exhausted",
            available_bytes=available_bytes,
            available_inodes=available_inodes,
        )
    if inodes_exhausted:
        return _emit(
            "CRITICAL",
            "inodes_exhausted",
            available_bytes=available_bytes,
            available_inodes=available_inodes,
        )

    return _emit(
        "OK",
        "none",
        available_bytes=available_bytes,
        available_inodes=available_inodes,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) > 1:
        return _emit("UNKNOWN", "invalid_arguments")
    path = args[0] if args else os.environ.get("IRLIGHT_FILESYSTEM_PATH", "/")
    return evaluate_filesystem(path)


if __name__ == "__main__":
    raise SystemExit(main())
