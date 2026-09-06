"""Durable initialization markers for JSON authority files.

An absent state file is valid only before its first authoritative write is
attempted. Once a store is about to publish authority, silently recreating it
as empty must never become possible again: doing so can resurrect credentials
or make provider resources look orphaned. A small sibling marker lets every
store distinguish those cases and fail closed after deletion or an interrupted
first commit.

Writers must fsync their temporary payload first, call ``mark_initialized`` to
arm the durable fuse, and only then replace the authority path. Crashing after
the fuse is armed but before ``os.replace`` intentionally leaves a fail-closed
state that requires recovery; the reverse order leaves a crash window where an
authoritative write can exist without durable evidence that it ever existed.
"""

from __future__ import annotations

import errno
import json
import os
import stat
from pathlib import Path
from typing import Any, Callable, TextIO


def _reject_duplicate_json_object_pairs(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key is not allowed")
        result[key] = value
    return result


def load_json_authority(
    handle: TextIO,
    *,
    parse_constant: Callable[[str], Any] | None = None,
) -> Any:
    """Load persisted authority JSON while rejecting ambiguous object keys.

    Python's default JSON decoder silently keeps the last value for duplicate
    object keys. Authority files must instead fail closed: duplicate keys can
    otherwise make the bytes an operator inspects differ from the object the
    service validates and acts on. The hook applies recursively to every JSON
    object, including nested records and event payloads.
    """

    kwargs: dict[str, Any] = {
        "object_pairs_hook": _reject_duplicate_json_object_pairs,
    }
    if parse_constant is not None:
        kwargs["parse_constant"] = parse_constant
    return json.load(handle, **kwargs)


def initialization_marker(path: Path) -> Path:
    return path.with_name(f".{path.name}.initialized")


def was_initialized(path: Path) -> bool:
    # Any directory entry is evidence of an attempted initialization. A broken
    # symlink or a non-regular marker must not enable empty-state recreation.
    # Permission/I/O errors deliberately propagate instead of meaning "absent".
    try:
        initialization_marker(path).lstat()
    except FileNotFoundError:
        return False
    return True


def mark_initialized(path: Path) -> None:
    """Durably arm the fuse that forbids implicit recreation of ``path``."""
    marker = initialization_marker(path)
    marker.parent.mkdir(parents=True, exist_ok=True)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o600)
        created = True
    except FileExistsError:
        existing = marker.lstat()
        if not stat.S_ISREG(existing.st_mode):
            raise OSError(errno.EINVAL, "initialization marker is not a regular file")
        # NONBLOCK prevents a raced replacement with a FIFO from hanging the
        # writer. fstat below validates the opened descriptor before any use.
        fd = os.open(marker, os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0))
        created = False
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError(errno.EINVAL, "initialization marker is not a regular file")
        if not created and (opened.st_dev, opened.st_ino) != (existing.st_dev, existing.st_ino):
            raise OSError(errno.EAGAIN, "initialization marker changed while opening")
        if created and os.write(fd, b"v1\n") != 3:
            raise OSError(errno.EIO, "initialization marker write was incomplete")
        # Existence alone does not prove durability: a previous attempt may
        # have failed its file or directory fsync. Retry both before returning,
        # without truncating an existing fuse (including a crash-created empty
        # one). The marker's presence, not its contents, is authoritative.
        os.fsync(fd)
    finally:
        os.close(fd)
    directory_fd = os.open(marker.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
