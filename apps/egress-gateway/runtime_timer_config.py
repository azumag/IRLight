from __future__ import annotations

import math
import os

from egress_report_cadence import liveness_window_can_be_armed


class RuntimeTimerConfigError(ValueError):
    """Raised when an Egress runtime timer cannot be interpreted safely."""


def finite_env_float(name: str, default: float) -> float:
    """Read one float environment value while rejecting non-finite values.

    Range semantics remain the caller's responsibility. This helper only
    hardens parsing so finite values keep their existing behavior while NaN,
    infinities, and malformed values fail closed without echoing the raw input.
    """

    raw = os.getenv(name)
    if raw is None:
        value = float(default)
    else:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise RuntimeTimerConfigError(
                f"{name} must be a finite number"
            ) from None

    if not math.isfinite(value):
        raise RuntimeTimerConfigError(f"{name} must be a finite number")
    return value


LIVENESS_TIMEOUT_NAME = "EGRESS_ISOLATED_LIVENESS_TIMEOUT_SECONDS"
STATUS_HEARTBEAT_NAME = "EGRESS_STATUS_HEARTBEAT_SECONDS"


def validate_liveness_window(
    liveness_timeout_seconds: float, status_heartbeat_seconds: float
) -> None:
    """Require an isolated liveness window strictly wider than the report cadence.

    The child's only post-connect liveness evidence is the status-heartbeat
    progress message, emitted on the existing 250 ms sink poll. The configured
    heartbeat is rounded up to that nominal poll cadence. A disabled heartbeat
    leaves no evidence channel, and a window no wider than the cadence expires
    between two healthy reports. Either combination fences an otherwise healthy
    attempt and drives the
    RECONNECTING loop this bound exists to shorten, so both fail closed here
    instead. A liveness window of `0` keeps the historical unbounded behavior
    and is not constrained.

    Neither raw value is echoed, matching the rest of the timer boundary.
    """

    if liveness_timeout_seconds <= 0:
        return
    if not liveness_window_can_be_armed(
        liveness_timeout_seconds, status_heartbeat_seconds
    ):
        raise RuntimeTimerConfigError(
            f"{LIVENESS_TIMEOUT_NAME} must be greater than the poll-rounded "
            f"{STATUS_HEARTBEAT_NAME} report interval"
        )
