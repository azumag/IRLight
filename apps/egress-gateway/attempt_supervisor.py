from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Mapping, Protocol


_REASON_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,99}$")
_ERROR_DOMAIN_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_ALLOWED_RESULT_KEYS = frozenset(
    {
        "reason_code",
        "connected_once",
        "rendered_buffers",
        "terminal",
        "error_domain",
        "error_code",
    }
)
_REQUIRED_RESULT_KEYS = frozenset(
    {"reason_code", "connected_once", "rendered_buffers", "terminal"}
)


class AttemptSupervisorError(RuntimeError):
    """Raised when an attempt child cannot be fenced and reaped safely."""


class InvalidAttemptResult(ValueError):
    """Raised when a child result violates the bounded IPC contract."""


class ChildProcess(Protocol):
    """Minimal process surface used by the egress attempt supervisor."""

    @property
    def exitcode(self) -> int | None: ...

    def is_alive(self) -> bool: ...

    def join(self, timeout: float | None = None) -> None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


@dataclass(frozen=True)
class ChildAttemptResult:
    reason_code: str
    connected_once: bool
    rendered_buffers: int
    terminal: bool
    error_domain: str | None = None
    error_code: int | None = None


@dataclass(frozen=True)
class ChildReapResult:
    disposition: str
    exit_code: int | None


def _bounded_timeout(value: object, *, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite non-negative number")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite non-negative number") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return parsed


def parse_child_attempt_result(payload: Mapping[str, Any]) -> ChildAttemptResult:
    """Validate the deliberately small, secret-free child result envelope."""

    if not isinstance(payload, Mapping):
        raise InvalidAttemptResult("child result must be an object")
    keys = frozenset(payload.keys())
    if not _REQUIRED_RESULT_KEYS.issubset(keys) or not keys.issubset(
        _ALLOWED_RESULT_KEYS
    ):
        raise InvalidAttemptResult("child result fields are invalid")

    reason_code = payload.get("reason_code")
    if not isinstance(reason_code, str) or not _REASON_RE.fullmatch(reason_code):
        raise InvalidAttemptResult("child reason_code is invalid")

    connected_once = payload.get("connected_once")
    terminal = payload.get("terminal")
    if type(connected_once) is not bool or type(terminal) is not bool:
        raise InvalidAttemptResult("child boolean fields are invalid")

    rendered_buffers = payload.get("rendered_buffers")
    if type(rendered_buffers) is not int or rendered_buffers < 0:
        raise InvalidAttemptResult("child rendered_buffers is invalid")

    error_domain = payload.get("error_domain")
    if error_domain is not None and (
        not isinstance(error_domain, str)
        or not _ERROR_DOMAIN_RE.fullmatch(error_domain)
    ):
        raise InvalidAttemptResult("child error_domain is invalid")

    error_code = payload.get("error_code")
    if error_code is not None and type(error_code) is not int:
        raise InvalidAttemptResult("child error_code is invalid")

    return ChildAttemptResult(
        reason_code=reason_code,
        connected_once=connected_once,
        rendered_buffers=rendered_buffers,
        terminal=terminal,
        error_domain=error_domain,
        error_code=error_code,
    )


def reap_child(
    process: ChildProcess,
    *,
    natural_timeout_seconds: float,
    terminate_timeout_seconds: float,
    kill_timeout_seconds: float,
) -> ChildReapResult:
    """Bound child teardown and never report success while the child is alive.

    The caller must not launch a replacement attempt until this function returns.
    If the process remains alive after the final kill grace, fail closed instead
    of pretending that the previous attempt has been fenced.
    """

    natural_timeout = _bounded_timeout(
        natural_timeout_seconds, name="natural_timeout_seconds"
    )
    terminate_timeout = _bounded_timeout(
        terminate_timeout_seconds, name="terminate_timeout_seconds"
    )
    kill_timeout = _bounded_timeout(kill_timeout_seconds, name="kill_timeout_seconds")

    process.join(natural_timeout)
    if not process.is_alive():
        return ChildReapResult("exited", process.exitcode)

    process.terminate()
    process.join(terminate_timeout)
    if not process.is_alive():
        return ChildReapResult("terminated", process.exitcode)

    process.kill()
    process.join(kill_timeout)
    if process.is_alive():
        raise AttemptSupervisorError("attempt child remained alive after kill")
    return ChildReapResult("killed", process.exitcode)
