from __future__ import annotations

import math
import os


class RuntimeTimerConfigError(ValueError):
    """Raised when a runtime timer cannot be interpreted safely."""


def finite_env_float(name: str, default: float) -> float:
    """Read one float environment value while rejecting non-finite values.

    The caller remains responsible for range semantics such as whether zero or
    negative values are meaningful. This helper intentionally changes only the
    malformed/non-finite boundary so existing finite-value behavior is kept.
    Raw environment values are never included in error output.
    """

    raw = os.getenv(name)
    if raw is None:
        value = float(default)
    else:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            # float() includes the raw input in its ValueError text. Suppress
            # that exception context so an uncaught configuration error cannot
            # echo the original environment value into startup logs.
            raise RuntimeTimerConfigError(
                f"{name} must be a finite number"
            ) from None

    if not math.isfinite(value):
        raise RuntimeTimerConfigError(f"{name} must be a finite number")
    return value
