"""Host-wide admission control for password KDF work.

Authentication registration and login deliberately perform PBKDF2 outside the
authority file lock so expensive password work cannot serialize unrelated state
operations.  That also means a process-local semaphore is not enough when a
Control Plane runs multiple Uvicorn worker processes.  This module bounds the
number of concurrent password KDF operations with non-blocking ``flock`` slots
shared by workers on the same runtime filesystem.

This gate limits concurrent expensive work only.  It is not a per-IP/account
rate limiter and it does not make a cluster-wide guarantee across independent
hosts unless operators intentionally provide a shared admission directory.
Lock files contain no email, password, user ID, token, or request data.
"""

from __future__ import annotations

import fcntl
import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


DEFAULT_MAX_CONCURRENT_KDFS = 4
MAX_CONCURRENT_KDFS_LIMIT = 32
DEFAULT_ADMISSION_DIR = "/tmp/irlight-auth-kdf-admission"


class AuthKdfAdmissionBusy(RuntimeError):
    """All configured password-KDF slots are currently held."""


class AuthKdfAdmissionUnavailable(RuntimeError):
    """The password-KDF admission lock set cannot be used safely."""


@dataclass(frozen=True)
class AuthKdfAdmissionConfig:
    max_concurrent: int = DEFAULT_MAX_CONCURRENT_KDFS
    lock_dir: Path = Path(DEFAULT_ADMISSION_DIR)

    def __post_init__(self) -> None:
        value = self.max_concurrent
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 1
            or value > MAX_CONCURRENT_KDFS_LIMIT
        ):
            raise ValueError("authentication KDF concurrency must be between 1 and 32")
        if not isinstance(self.lock_dir, Path) or not self.lock_dir.is_absolute():
            raise ValueError("authentication KDF admission directory must be an absolute path")

    @classmethod
    def from_env(cls) -> "AuthKdfAdmissionConfig":
        raw_limit = os.getenv(
            "IRLIGHT_AUTH_KDF_MAX_CONCURRENT", str(DEFAULT_MAX_CONCURRENT_KDFS)
        )
        try:
            parsed = int(raw_limit, 10)
        except (TypeError, ValueError):
            parsed = DEFAULT_MAX_CONCURRENT_KDFS
        if parsed < 1 or parsed > MAX_CONCURRENT_KDFS_LIMIT:
            parsed = DEFAULT_MAX_CONCURRENT_KDFS

        raw_dir = os.getenv("IRLIGHT_AUTH_KDF_ADMISSION_DIR", DEFAULT_ADMISSION_DIR)
        lock_dir = Path(raw_dir)
        if not lock_dir.is_absolute():
            lock_dir = Path(DEFAULT_ADMISSION_DIR)
        return cls(max_concurrent=parsed, lock_dir=lock_dir)


def _prepare_lock_dir(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        stat_result = path.lstat()
    except OSError as exc:
        raise AuthKdfAdmissionUnavailable(
            "authentication KDF admission is unavailable"
        ) from exc
    if stat.S_ISLNK(stat_result.st_mode) or not stat.S_ISDIR(stat_result.st_mode):
        raise AuthKdfAdmissionUnavailable(
            "authentication KDF admission is unavailable"
        )


def _open_slot(path: Path) -> int:
    flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise AuthKdfAdmissionUnavailable(
                "authentication KDF admission is unavailable"
            )
        return fd
    except AuthKdfAdmissionUnavailable:
        raise
    except OSError as exc:
        raise AuthKdfAdmissionUnavailable(
            "authentication KDF admission is unavailable"
        ) from exc


@contextmanager
def auth_kdf_slot(
    config: AuthKdfAdmissionConfig | None = None,
) -> Iterator[None]:
    """Acquire one non-blocking host-wide KDF slot or fail immediately."""

    cfg = config or AuthKdfAdmissionConfig.from_env()
    _prepare_lock_dir(cfg.lock_dir)

    acquired_fd: int | None = None
    try:
        for index in range(cfg.max_concurrent):
            fd = _open_slot(cfg.lock_dir / f"slot-{index}.lock")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                continue
            except OSError as exc:
                os.close(fd)
                raise AuthKdfAdmissionUnavailable(
                    "authentication KDF admission is unavailable"
                ) from exc
            acquired_fd = fd
            break

        if acquired_fd is None:
            raise AuthKdfAdmissionBusy("authentication KDF capacity is busy")
        yield
    finally:
        if acquired_fd is not None:
            try:
                fcntl.flock(acquired_fd, fcntl.LOCK_UN)
            finally:
                os.close(acquired_fd)
