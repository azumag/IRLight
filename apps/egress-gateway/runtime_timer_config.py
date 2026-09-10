from __future__ import annotations

import math
import os


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
