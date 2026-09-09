"""Read-only secret file permission inspection for Media Node operators.

The inspector deliberately uses metadata only. It never opens or reads a secret
file, so the resulting JSON can be attached to an incident without copying
credential material into logs or tickets.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
from pathlib import Path
from typing import Any


class SecretFileInspectError(RuntimeError):
    """Raised when the requested inspection cannot be performed safely."""


def _identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def inspect_secret_path(path: Path) -> dict[str, Any]:
    """Inspect one secret path without following symlinks or reading content."""

    result: dict[str, Any] = {
        "path": str(path),
        "status": "PROBLEM",
        "problems": [],
    }
    problems: list[str] = result["problems"]

    try:
        parent_before = path.parent.lstat()
    except OSError:
        problems.append("parent_unavailable")
        return result

    if stat.S_ISLNK(parent_before.st_mode):
        problems.append("parent_symlink")
        return result
    if not stat.S_ISDIR(parent_before.st_mode):
        problems.append("parent_not_directory")
        return result

    parent_mode = stat.S_IMODE(parent_before.st_mode)
    result["parent_mode"] = f"{parent_mode:04o}"
    if parent_mode & 0o077:
        problems.append("parent_permissions_too_open")

    open_flags = os.O_RDONLY
    open_flags |= getattr(os, "O_DIRECTORY", 0)
    open_flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        parent_fd = os.open(path.parent, open_flags)
    except OSError:
        problems.append("parent_unavailable")
        return result

    try:
        parent_open = os.fstat(parent_fd)
        if (
            not stat.S_ISDIR(parent_open.st_mode)
            or _identity(parent_open) != _identity(parent_before)
        ):
            problems.append("parent_changed")
            return result

        try:
            file_state = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            problems.append("missing")
            return result
        except OSError:
            problems.append("file_unavailable")
            return result

        if stat.S_ISLNK(file_state.st_mode):
            problems.append("symlink")
            return result
        if not stat.S_ISREG(file_state.st_mode):
            problems.append("not_regular_file")
            return result

        file_mode = stat.S_IMODE(file_state.st_mode)
        result["file_mode"] = f"{file_mode:04o}"
        if file_mode & 0o077:
            problems.append("permissions_too_open")

        try:
            file_after = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            problems.append("file_changed")
            return result
        if (
            _identity(file_after) != _identity(file_state)
            or file_after.st_mode != file_state.st_mode
            or not stat.S_ISREG(file_after.st_mode)
        ):
            problems.append("file_changed")
            return result

        try:
            parent_after = path.parent.lstat()
        except OSError:
            problems.append("parent_changed")
            return result
        if (
            stat.S_ISLNK(parent_after.st_mode)
            or not stat.S_ISDIR(parent_after.st_mode)
            or _identity(parent_after) != _identity(parent_open)
        ):
            problems.append("parent_changed")
            return result
    finally:
        os.close(parent_fd)

    result["status"] = "OK" if not problems else "PROBLEM"
    return result


def inspect_secret_paths(paths: list[Path]) -> dict[str, Any]:
    if not paths:
        raise SecretFileInspectError("at least one secret path is required")

    inspected = [inspect_secret_path(path) for path in paths]
    problem_count = sum(item["status"] != "OK" for item in inspected)
    return {
        "status": "PROBLEM" if problem_count else "OK",
        "problem_count": problem_count,
        "files": inspected,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="secret_file_inspect_cli")
    parser.add_argument(
        "--path",
        action="append",
        dest="paths",
        default=[],
        help="Secret file path to inspect; repeat for multiple files",
    )
    args = parser.parse_args(argv)
    if not args.paths:
        parser.error("at least one --path is required")

    try:
        result = inspect_secret_paths([Path(value) for value in args.paths])
    except SecretFileInspectError as exc:
        print(json.dumps({"status": "ERROR", "reason": str(exc)}, sort_keys=True))
        return 1

    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0 if result["status"] == "OK" else 2


if __name__ == "__main__":
    raise SystemExit(main())
