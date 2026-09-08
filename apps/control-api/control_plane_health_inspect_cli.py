"""Read-only local Control Plane liveness/readiness inspection for operators.

The inspector only issues GET requests to loopback addresses. It intentionally
rejects credentials, non-loopback hosts, redirects, query strings and fragments
so an operator diagnostic cannot become an SSRF or credential exfiltration path.
Response bodies are never printed.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


DEFAULT_BASE_URL = "http://127.0.0.1:8080"
LOOPBACK_HOSTS = {"127.0.0.1", "::1"}


class ControlPlaneProbeError(RuntimeError):
    """Raised when a bounded local health probe cannot obtain an HTTP status."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


def _local_base_url(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise argparse.ArgumentTypeError("invalid base URL") from exc

    if parsed.scheme not in {"http", "https"}:
        raise argparse.ArgumentTypeError("base URL scheme must be http or https")
    if parsed.hostname is None or parsed.hostname.lower() not in LOOPBACK_HOSTS:
        raise argparse.ArgumentTypeError("base URL host must be loopback")
    if parsed.username is not None or parsed.password is not None:
        raise argparse.ArgumentTypeError("base URL must not contain credentials")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise argparse.ArgumentTypeError("base URL must not contain path, query, or fragment")
    if port is not None and not (1 <= port <= 65535):
        raise argparse.ArgumentTypeError("base URL port is out of range")
    return value.rstrip("/")


def _bounded_timeout(value: str) -> float:
    try:
        numeric = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a number") from exc
    if not math.isfinite(numeric) or numeric <= 0 or numeric > 10:
        raise argparse.ArgumentTypeError(
            "timeout must be a finite positive number no greater than 10 seconds"
        )
    return numeric


def _probe_http_status(
    opener: urllib.request.OpenerDirector,
    url: str,
    *,
    timeout_seconds: float,
) -> int:
    request = urllib.request.Request(
        url,
        method="GET",
        headers={"User-Agent": "IRLight-control-plane-health-inspect/1"},
    )
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        # HTTP failures are useful classification signals, but the response body
        # is deliberately ignored so diagnostics cannot echo internal details.
        return int(exc.code)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ControlPlaneProbeError("local Control Plane probe failed") from exc


def inspect_control_plane(
    base_url: str,
    *,
    timeout_seconds: float,
    opener: urllib.request.OpenerDirector | None = None,
) -> tuple[int, dict[str, Any]]:
    """Classify local process liveness and application readiness without mutation."""
    client = opener or urllib.request.build_opener(_NoRedirect())

    try:
        liveness_status = _probe_http_status(
            client,
            f"{base_url}/healthz",
            timeout_seconds=timeout_seconds,
        )
    except ControlPlaneProbeError:
        return 3, {
            "status": "UNAVAILABLE",
            "reason": "CONTROL_PLANE_UNAVAILABLE",
            "liveness_http_status": None,
            "readiness_http_status": None,
        }

    if not 200 <= liveness_status < 300:
        return 3, {
            "status": "UNAVAILABLE",
            "reason": "LIVENESS_FAILED",
            "liveness_http_status": liveness_status,
            "readiness_http_status": None,
        }

    try:
        readiness_status = _probe_http_status(
            client,
            f"{base_url}/readyz",
            timeout_seconds=timeout_seconds,
        )
    except ControlPlaneProbeError:
        return 3, {
            "status": "UNAVAILABLE",
            "reason": "READINESS_UNAVAILABLE",
            "liveness_http_status": liveness_status,
            "readiness_http_status": None,
        }

    if not 200 <= readiness_status < 300:
        return 2, {
            "status": "NOT_READY",
            "reason": "READINESS_FAILED",
            "liveness_http_status": liveness_status,
            "readiness_http_status": readiness_status,
        }

    return 0, {
        "status": "READY",
        "reason": "CONTROL_PLANE_READY",
        "liveness_http_status": liveness_status,
        "readiness_http_status": readiness_status,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="control_plane_health_inspect_cli")
    parser.add_argument(
        "--base-url",
        type=_local_base_url,
        default=os.getenv("CONTROL_PLANE_LOCAL_URL", DEFAULT_BASE_URL),
        help="loopback Control Plane base URL (default: env or http://127.0.0.1:8080)",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=_bounded_timeout,
        default=os.getenv("CONTROL_PLANE_HEALTH_TIMEOUT_SECONDS", "3"),
        help="per-request timeout, capped at 10 seconds (default: env or 3)",
    )
    args = parser.parse_args(argv)

    exit_code, payload = inspect_control_plane(
        args.base_url,
        timeout_seconds=args.timeout_seconds,
    )
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
