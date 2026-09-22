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


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _safe_lock_dir(stat_result: os.stat_result) -> bool:
    return (
        not stat.S_ISLNK(stat_result.st_mode)
        and stat.S_ISDIR(stat_result.st_mode)
        and stat_result.st_uid == os.geteuid()
        and not stat_result.st_mode & 0o077
    )


def _safe_slot(stat_result: os.stat_result) -> bool:
    return (
        stat.S_ISREG(stat_result.st_mode)
        and stat_result.st_uid == os.geteuid()
        and not stat_result.st_mode & 0o077
    )


def _open_lock_dir(path: Path) -> int:
    """Create, validate, and pin the configured admission directory."""

    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path_stat = path.lstat()
    except OSError as exc:
        raise AuthKdfAdmissionUnavailable(
            "authentication KDF admission is unavailable"
        ) from exc
    if not _safe_lock_dir(path_stat):
        raise AuthKdfAdmissionUnavailable(
            "authentication KDF admission is unavailable"
        )

    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd: int | None = None
    try:
        fd = os.open(path, flags)
        opened_stat = os.fstat(fd)
        if not _safe_lock_dir(opened_stat) or not _same_file(path_stat, opened_stat):
            raise AuthKdfAdmissionUnavailable(
                "authentication KDF admission is unavailable"
            )
        return fd
    except AuthKdfAdmissionUnavailable:
        if fd is not None:
            os.close(fd)
        raise
    except OSError as exc:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        raise AuthKdfAdmissionUnavailable(
            "authentication KDF admission is unavailable"
        ) from exc


def _lock_dir_matches_path(path: Path, lock_dir_fd: int) -> bool:
    try:
        path_stat = path.lstat()
        opened_stat = os.fstat(lock_dir_fd)
    except OSError:
        return False
    return (
        _safe_lock_dir(path_stat)
        and _safe_lock_dir(opened_stat)
        and _same_file(path_stat, opened_stat)
    )


def _open_slot(lock_dir_fd: int, slot_name: str) -> int:
    flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd: int | None = None
    try:
        fd = os.open(slot_name, flags, 0o600, dir_fd=lock_dir_fd)
        if not _safe_slot(os.fstat(fd)):
            raise AuthKdfAdmissionUnavailable(
                "authentication KDF admission is unavailable"
            )
        return fd
    except AuthKdfAdmissionUnavailable:
        if fd is not None:
            os.close(fd)
        raise
    except OSError as exc:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        raise AuthKdfAdmissionUnavailable(
            "authentication KDF admission is unavailable"
        ) from exc


def _slot_matches_path(lock_dir_fd: int, slot_name: str, slot_fd: int) -> bool:
    try:
        path_stat = os.stat(
            slot_name,
            dir_fd=lock_dir_fd,
            follow_symlinks=False,
        )
        opened_stat = os.fstat(slot_fd)
    except OSError:
        return False
    return (
        _safe_slot(path_stat)
        and _safe_slot(opened_stat)
        and _same_file(path_stat, opened_stat)
    )


@contextmanager
def auth_kdf_slot(
    config: AuthKdfAdmissionConfig | None = None,
) -> Iterator[None]:
    """Acquire one non-blocking host-wide KDF slot or fail immediately."""

    cfg = config or AuthKdfAdmissionConfig.from_env()
    lock_dir_fd = _open_lock_dir(cfg.lock_dir)

    acquired_fd: int | None = None
    acquired_name: str | None = None
    try:
        for index in range(cfg.max_concurrent):
            slot_name = f"slot-{index}.lock"
            fd = _open_slot(lock_dir_fd, slot_name)
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
            if not _slot_matches_path(lock_dir_fd, slot_name, fd):
                os.close(fd)
                raise AuthKdfAdmissionUnavailable(
                    "authentication KDF admission is unavailable"
                )
            acquired_fd = fd
            acquired_name = slot_name
            break

        if acquired_fd is None:
            raise AuthKdfAdmissionBusy("authentication KDF capacity is busy")
        if (
            acquired_name is None
            or not _lock_dir_matches_path(cfg.lock_dir, lock_dir_fd)
            or not _slot_matches_path(lock_dir_fd, acquired_name, acquired_fd)
        ):
            raise AuthKdfAdmissionUnavailable(
                "authentication KDF admission is unavailable"
            )
        yield
    finally:
        if acquired_fd is not None:
            try:
                fcntl.flock(acquired_fd, fcntl.LOCK_UN)
            finally:
                os.close(acquired_fd)
        os.close(lock_dir_fd)
