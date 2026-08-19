from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit


TERMINAL_REASON_CODES = {
    "AUTH_FAILED",
    "PUBLISH_CONFLICT",
    "PUBLISH_REJECTED",
    "LOCAL_PIPELINE_FAILED",
}


def safe_destination(url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    return parsed.scheme.lower(), parsed.hostname or ""


def classify_error(
    *,
    source_name: str,
    message: str,
    debug: str | None = None,
    error_domain: str | None = None,
    error_code: int | None = None,
    connected_once: bool = False,
) -> str:
    """Map transport errors to stable codes without returning raw text."""
    if source_name.startswith("src") or "rtspsrc" in source_name:
        return "UPSTREAM_UNAVAILABLE"

    haystack = f"{message}\n{debug or ''}".lower()
    if any(
        token in haystack
        for token in (
            "unauthorized",
            "forbidden",
            "auth failed",
            "authfailed",
            "invalid stream key",
            " 401 ",
            " 403 ",
            "code=401",
            "code=403",
        )
    ):
        return "AUTH_FAILED"
    if any(
        token in haystack
        for token in ("badname", "already publishing", "publish conflict", "stream already")
    ):
        return "PUBLISH_CONFLICT"
    if any(token in haystack for token in ("certificate", "tls", "ssl")):
        return "TLS_FAILED"
    if any(
        token in haystack
        for token in (
            "could not resolve",
            "name or service not known",
            "temporary failure in name resolution",
            "dns",
        )
    ):
        return "DNS_FAILED"
    if "timeout" in haystack or "timed out" in haystack:
        return "TIMEOUT"
    if any(
        token in haystack
        for token in (
            "connection refused",
            "network is unreachable",
            "no route to host",
            "could not connect",
            "connection reset",
        )
    ):
        return "UNREACHABLE"

    # GStreamer's rtmpsink/librtmp path can discard the server's RTMP publish
    # rejection text and surface only Gst.ResourceError.WRITE (code 10). Treat
    # that as a terminal publish rejection only before the first successful
    # rendered buffer. The same WRITE error after a successful connection is a
    # transport outage and must remain retryable.
    if (
        source_name == "egress_sink"
        and not connected_once
        and error_domain == "gst-resource-error-quark"
        and error_code == 10
    ):
        return "PUBLISH_REJECTED"

    return "EGRESS_PIPELINE_FAILED"


@dataclass(frozen=True)
class ReconnectPolicy:
    initial_seconds: float = 1.0
    max_seconds: float = 30.0
    multiplier: float = 2.0
    jitter_ratio: float = 0.2
    max_attempts: int = 0
    max_elapsed_seconds: float = 0.0

    def __post_init__(self) -> None:
        if self.initial_seconds < 0 or self.max_seconds < 0:
            raise ValueError("retry delays must be non-negative")
        if self.max_seconds < self.initial_seconds:
            raise ValueError("max retry delay must be >= initial retry delay")
        if self.multiplier < 1.0:
            raise ValueError("retry multiplier must be >= 1")
        if not 0.0 <= self.jitter_ratio <= 1.0:
            raise ValueError("retry jitter ratio must be between 0 and 1")
        if self.max_attempts < 0 or self.max_elapsed_seconds < 0:
            raise ValueError("retry limits must be non-negative")

    def delay_for(self, failure_count: int, random_value: float = 0.5) -> float:
        exponent = max(0, failure_count - 1)
        base = min(self.max_seconds, self.initial_seconds * (self.multiplier**exponent))
        bounded_random = min(1.0, max(0.0, random_value))
        factor = 1.0 + ((bounded_random * 2.0) - 1.0) * self.jitter_ratio
        return max(0.0, base * factor)

    def exhausted(self, failure_count: int, elapsed_seconds: float) -> bool:
        if self.max_attempts > 0 and failure_count >= self.max_attempts:
            return True
        if self.max_elapsed_seconds > 0 and elapsed_seconds >= self.max_elapsed_seconds:
            return True
        return False
