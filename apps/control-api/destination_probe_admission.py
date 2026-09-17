"""Host-wide admission control for active Destination verification probes.

The verification route performs outbound DNS / TCP / TLS / SRT work. Uvicorn
executes synchronous routes in a worker thread, and deployments may use more
than one worker process, so a process-local semaphore alone does not provide a
useful bound. This module uses non-blocking ``flock`` leases on a small set of
slot files. The kernel releases the lease automatically when a worker exits,
which avoids stale lease records after crashes.

This is deliberately an admission gate only. It does not change Destination
state, start Media Nodes, call providers, or retry probes. Cluster-wide limits
across hosts/containers remain a deployment concern; the default lock directory
is shared by all workers in one Control Plane container/host and can be moved to
a shared runtime filesystem explicitly.
"""

from __future__ import annotations

import fcntl
import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


DEFAULT_MAX_CONCURRENT_PROBES = 4
MAX_CONCURRENT_PROBES_LIMIT = 32
DEFAULT_ADMISSION_DIR = "/tmp/irlight-destination-probe-admission"


class DestinationProbeAdmissionBusy(RuntimeError):
    """All configured probe slots are currently held."""


class DestinationProbeAdmissionUnavailable(RuntimeError):
    """The admission-control lock set cannot be used safely."""


@dataclass(frozen=True)
class DestinationProbeAdmissionConfig:
    max_concurrent: int = DEFAULT_MAX_CONCURRENT_PROBES
    lock_dir: Path = Path(DEFAULT_ADMISSION_DIR)

    def __post_init__(self) -> None:
        value = self.max_concurrent
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 1
            or value > MAX_CONCURRENT_PROBES_LIMIT
        ):
            raise ValueError("probe concurrency must be between 1 and 32")
        if not isinstance(self.lock_dir, Path) or not self.lock_dir.is_absolute():
            raise ValueError("probe admission directory must be an absolute path")

    @classmethod
    def from_env(cls) -> "DestinationProbeAdmissionConfig":
        raw_limit = os.getenv(
            "IRLIGHT_VERIFY_MAX_CONCURRENT", str(DEFAULT_MAX_CONCURRENT_PROBES)
        )
        try:
            parsed = int(raw_limit, 10)
        except (TypeError, ValueError):
            parsed = DEFAULT_MAX_CONCURRENT_PROBES
        if parsed < 1 or parsed > MAX_CONCURRENT_PROBES_LIMIT:
            parsed = DEFAULT_MAX_CONCURRENT_PROBES

        raw_dir = os.getenv("IRLIGHT_VERIFY_ADMISSION_DIR", DEFAULT_ADMISSION_DIR)
        lock_dir = Path(raw_dir)
        if not lock_dir.is_absolute():
            lock_dir = Path(DEFAULT_ADMISSION_DIR)
        return cls(max_concurrent=parsed, lock_dir=lock_dir)


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _open_lock_dir(path: Path) -> int:
    """Create and pin the configured admission directory without following it."""

    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path_stat = path.lstat()
    except OSError as exc:
        raise DestinationProbeAdmissionUnavailable(
            "destination verification admission is unavailable"
        ) from exc
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISDIR(path_stat.st_mode):
        raise DestinationProbeAdmissionUnavailable(
            "destination verification admission is unavailable"
        )

    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
        opened_stat = os.fstat(fd)
        if not stat.S_ISDIR(opened_stat.st_mode) or not _same_file(path_stat, opened_stat):
            os.close(fd)
            raise DestinationProbeAdmissionUnavailable(
                "destination verification admission is unavailable"
            )
        return fd
    except DestinationProbeAdmissionUnavailable:
        raise
    except OSError as exc:
        raise DestinationProbeAdmissionUnavailable(
            "destination verification admission is unavailable"
        ) from exc


def _lock_dir_matches_path(path: Path, lock_dir_fd: int) -> bool:
    try:
        path_stat = path.lstat()
        opened_stat = os.fstat(lock_dir_fd)
    except OSError:
        return False
    return (
        not stat.S_ISLNK(path_stat.st_mode)
        and stat.S_ISDIR(path_stat.st_mode)
        and stat.S_ISDIR(opened_stat.st_mode)
        and _same_file(path_stat, opened_stat)
    )


def _open_slot(lock_dir_fd: int, slot_name: str) -> int:
    flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(slot_name, flags, 0o600, dir_fd=lock_dir_fd)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise DestinationProbeAdmissionUnavailable(
                "destination verification admission is unavailable"
            )
        return fd
    except DestinationProbeAdmissionUnavailable:
        raise
    except OSError as exc:
        raise DestinationProbeAdmissionUnavailable(
            "destination verification admission is unavailable"
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
        stat.S_ISREG(path_stat.st_mode)
        and stat.S_ISREG(opened_stat.st_mode)
        and _same_file(path_stat, opened_stat)
    )


@contextmanager
def destination_probe_slot(
    config: DestinationProbeAdmissionConfig | None = None,
) -> Iterator[None]:
    """Acquire one non-blocking host-wide probe slot or fail immediately."""

    cfg = config or DestinationProbeAdmissionConfig.from_env()
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
                raise DestinationProbeAdmissionUnavailable(
                    "destination verification admission is unavailable"
                ) from exc
            if not _slot_matches_path(lock_dir_fd, slot_name, fd):
                os.close(fd)
                raise DestinationProbeAdmissionUnavailable(
                    "destination verification admission is unavailable"
                )
            acquired_fd = fd
            acquired_name = slot_name
            break

        if acquired_fd is None:
            raise DestinationProbeAdmissionBusy("destination verification is busy")
        if (
            acquired_name is None
            or not _lock_dir_matches_path(cfg.lock_dir, lock_dir_fd)
            or not _slot_matches_path(lock_dir_fd, acquired_name, acquired_fd)
        ):
            raise DestinationProbeAdmissionUnavailable(
                "destination verification admission is unavailable"
            )
        yield
    finally:
        if acquired_fd is not None:
            try:
                fcntl.flock(acquired_fd, fcntl.LOCK_UN)
            finally:
                os.close(acquired_fd)
        os.close(lock_dir_fd)
