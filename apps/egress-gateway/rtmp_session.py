from __future__ import annotations

import math


DEFAULT_LIBRTMP_SESSION_TIMEOUT_SECONDS = 30
MAX_LIBRTMP_SESSION_TIMEOUT_SECONDS = 300


def parse_librtmp_session_timeout(raw: str | None) -> int:
    """Return a bounded librtmp session timeout in whole seconds.

    A non-positive value disables the explicit librtmp timeout parameter and
    leaves librtmp's own default in effect.
    """
    if raw is None or not raw.strip():
        return DEFAULT_LIBRTMP_SESSION_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError("librtmp session timeout must be numeric") from exc
    if not math.isfinite(value):
        raise ValueError("librtmp session timeout must be finite")
    if value <= 0:
        return 0
    return min(MAX_LIBRTMP_SESSION_TIMEOUT_SECONDS, max(1, math.ceil(value)))


def with_librtmp_session_timeout(url: str, timeout_seconds: int) -> str:
    """Add the librtmp timeout session parameter without exposing the URL."""
    if not url or any(character.isspace() for character in url):
        raise ValueError("RTMP URL must not contain whitespace")
    if timeout_seconds <= 0:
        return url
    if timeout_seconds > MAX_LIBRTMP_SESSION_TIMEOUT_SECONDS:
        raise ValueError("librtmp session timeout exceeds maximum")
    return f"{url} timeout={timeout_seconds}"
