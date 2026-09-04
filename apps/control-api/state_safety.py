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

import os
from pathlib import Path


def initialization_marker(path: Path) -> Path:
    return path.with_name(f".{path.name}.initialized")


def was_initialized(path: Path) -> bool:
    return initialization_marker(path).is_file()


def mark_initialized(path: Path) -> None:
    """Durably arm the fuse that forbids implicit recreation of ``path``."""
    marker = initialization_marker(path)
    if marker.is_file():
        return
    marker.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(marker, flags, 0o600)
    try:
        os.ftruncate(fd, 0)
        os.write(fd, b"v1\n")
        os.fsync(fd)
    finally:
        os.close(fd)
    directory_fd = os.open(marker.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)