"""Host-wide admission control for active Destination verification probes.

The verification route performs outbound DNS / TCP / TLS / SRT work.  Uvicorn
executes synchronous routes in a worker thread, and deployments may use more
than one worker process, so a process-local semaphore alone does not provide a
useful bound.  This module uses non-blocking ``flock`` leases on a small set of
slot files.  The kernel releases the lease automatically when a worker exits,
which avoids stale lease records after crashes.

This is deliberately an admission gate only.  It does not change Destination
state, start Media Nodes, call providers, or retry probes.  Cluster-wide limits
across hosts/containers remain a deployment concern; the default lock directory
is shared by all workers in one Control Plane container/host and can be moved to
a shared runtime filesystem explicitly.
"""

from __future__ import annotations

import fcntl
import math
import os
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


def _prepare_lock_dir(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        stat_result = path.lstat()
    except OSError as exc:
        raise DestinationProbeAdmissionUnavailable(
            "destination verification admission is unavailable"
        ) from exc
    if not path.is_dir() or path.is_symlink():
        raise DestinationProbeAdmissionUnavailable(
            "destination verification admission is unavailable"
        )
    # Do not require an exact mode because an operator may intentionally use a
    # shared runtime directory with a stricter umask/ACL.  The individual slot
    # files are opened with mode 0600 and contain no request or credential data.
    if not math.isfinite(float(stat_result.st_nlink)):
        # Defensive type sanity for unusual stat implementations/test doubles.
        raise DestinationProbeAdmissionUnavailable(
            "destination verification admission is unavailable"
        )


@contextmanager
def destination_probe_slot(
    config: DestinationProbeAdmissionConfig | None = None,
) -> Iterator[None]:
    """Acquire one non-blocking host-wide probe slot or fail immediately."""

    cfg = config or DestinationProbeAdmissionConfig.from_env()
    _prepare_lock_dir(cfg.lock_dir)

    acquired_fd: int | None = None
    try:
        for index in range(cfg.max_concurrent):
            path = cfg.lock_dir / f"slot-{index}.lock"
            try:
                fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
            except OSError as exc:
                raise DestinationProbeAdmissionUnavailable(
                    "destination verification admission is unavailable"
                ) from exc
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
            acquired_fd = fd
            break

        if acquired_fd is None:
            raise DestinationProbeAdmissionBusy("destination verification is busy")
        yield
    finally:
        if acquired_fd is not None:
            try:
                fcntl.flock(acquired_fd, fcntl.LOCK_UN)
            finally:
                os.close(acquired_fd)
