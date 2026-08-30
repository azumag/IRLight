"""Durable initialization markers for JSON authority files.

An absent state file is valid only before its first successful write.  Once a
file has been initialized, silently recreating it as empty can resurrect
credentials or make provider resources look orphaned.  A small sibling marker
lets every store distinguish those cases and fail closed after deletion.
"""

from __future__ import annotations

import os
from pathlib import Path


def initialization_marker(path: Path) -> Path:
    return path.with_name(f".{path.name}.initialized")


def was_initialized(path: Path) -> bool:
    return initialization_marker(path).is_file()


def mark_initialized(path: Path) -> None:
    """Durably record that ``path`` must never be recreated implicitly."""
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
