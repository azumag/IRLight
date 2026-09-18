"""Bounded, non-blocking reads for Node Agent runtime secret files.

The paths are operator-controlled configuration, not public request input, but
secret readers still need a small fail-closed boundary: a mistaken FIFO/device
must not block the Node Agent, oversized files must not be read without bound,
and diagnostics must not expose secret paths or raw OS errors.

Symlinks to regular files remain supported for Docker/Kubernetes projected
secret-volume compatibility.  The resolved target identity is checked before,
during, and after the read so target replacement or in-place mutation is not
silently accepted.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path


MAX_RUNTIME_SECRET_BYTES = 64 * 1024
_READ_CHUNK_BYTES = 64 * 1024


class RuntimeSecretFileError(RuntimeError):
    """Raised when a runtime secret cannot be read through the safe boundary."""


def _snapshot(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _unavailable() -> RuntimeSecretFileError:
    return RuntimeSecretFileError("runtime secret file is unavailable")


def read_runtime_secret(
    path: Path,
    *,
    max_bytes: int = MAX_RUNTIME_SECRET_BYTES,
) -> str:
    """Read one UTF-8 secret from a stable regular-file snapshot.

    ``path.stat()`` intentionally follows a final symlink.  This preserves the
    projected-secret contract while still comparing the resolved target with
    the file descriptor opened immediately afterwards and with the final path
    resolution once reading has finished.
    """

    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")

    try:
        inspected = path.stat()
    except OSError:
        raise _unavailable() from None

    if not stat.S_ISREG(inspected.st_mode):
        raise RuntimeSecretFileError("runtime secret file is not a regular file")
    if inspected.st_size > max_bytes:
        raise RuntimeSecretFileError("runtime secret file exceeds size limit")

    flags = os.O_RDONLY
    flags |= getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        raise _unavailable() from None

    try:
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise RuntimeSecretFileError(
                    "runtime secret file is not a regular file"
                )
            if _snapshot(opened) != _snapshot(inspected):
                raise RuntimeSecretFileError("runtime secret file changed during read")
            if opened.st_size > max_bytes:
                raise RuntimeSecretFileError("runtime secret file exceeds size limit")

            payload = bytearray()
            remaining = max_bytes + 1
            while remaining > 0:
                chunk = os.read(fd, min(_READ_CHUNK_BYTES, remaining))
                if not chunk:
                    break
                payload.extend(chunk)
                remaining -= len(chunk)

            if len(payload) > max_bytes:
                raise RuntimeSecretFileError("runtime secret file exceeds size limit")

            after = os.fstat(fd)
            try:
                resolved_after = path.stat()
            except OSError:
                raise RuntimeSecretFileError(
                    "runtime secret file changed during read"
                ) from None
            if (
                _snapshot(after) != _snapshot(opened)
                or _snapshot(resolved_after) != _snapshot(after)
            ):
                raise RuntimeSecretFileError("runtime secret file changed during read")
        except RuntimeSecretFileError:
            raise
        except OSError:
            raise _unavailable() from None
    finally:
        try:
            os.close(fd)
        except OSError:
            pass

    try:
        return bytes(payload).decode("utf-8")
    except UnicodeDecodeError:
        raise RuntimeSecretFileError("runtime secret file is not valid UTF-8") from None
