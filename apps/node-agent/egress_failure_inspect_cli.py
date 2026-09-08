"""Secret-safe, read-only egress failure diagnostics for operators.

The inspector reads only the already-redacted egress status JSON produced by
Egress Gateway. It does not contact a destination, read the destination secret,
or start/stop/restart any service. Unknown reason strings are never echoed.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from pathlib import Path
from typing import Any

from egress_status import read_egress_status


SAFE_REASON_CODES = {
    "AUTH_FAILED",
    "PUBLISH_CONFLICT",
    "PUBLISH_REJECTED",
    "LOCAL_PIPELINE_FAILED",
    "SECRET_UNAVAILABLE",
    "TLS_FAILED",
    "DNS_FAILED",
    "TIMEOUT",
    "UNREACHABLE",
    "UPSTREAM_UNAVAILABLE",
    "UPSTREAM_EOS",
    "EGRESS_PIPELINE_FAILED",
    "RETRY_EXHAUSTED",
    "STATUS_UNAVAILABLE",
    "STATUS_INVALID",
    "STATUS_STALE",
    "STATUS_INCONSISTENT",
}

ACTION_BY_REASON = {
    "AUTH_FAILED": "CHECK_DESTINATION_CREDENTIAL",
    "PUBLISH_CONFLICT": "CHECK_PUBLISH_CONFLICT",
    "PUBLISH_REJECTED": "CHECK_DESTINATION_POLICY",
    "TLS_FAILED": "CHECK_DESTINATION_TLS",
    "DNS_FAILED": "CHECK_DESTINATION_DNS",
    "TIMEOUT": "CHECK_DESTINATION_NETWORK",
    "UNREACHABLE": "CHECK_DESTINATION_NETWORK",
    "SECRET_UNAVAILABLE": "CHECK_SECRET_DELIVERY",
    "LOCAL_PIPELINE_FAILED": "CHECK_LOCAL_MEDIA_PATH",
    "EGRESS_PIPELINE_FAILED": "CHECK_LOCAL_MEDIA_PATH",
    "UPSTREAM_UNAVAILABLE": "CHECK_LOCAL_MEDIA_PATH",
    "UPSTREAM_EOS": "CHECK_LOCAL_MEDIA_PATH",
    "RETRY_EXHAUSTED": "CHECK_RETRY_EXHAUSTION",
    "STATUS_UNAVAILABLE": "CHECK_EGRESS_STATUS_SOURCE",
    "STATUS_INVALID": "CHECK_EGRESS_STATUS_SOURCE",
    "STATUS_STALE": "CHECK_EGRESS_STATUS_SOURCE",
    "STATUS_INCONSISTENT": "CHECK_EGRESS_STATUS_SOURCE",
}


def _positive_finite(value: str) -> float:
    try:
        numeric = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(numeric) or numeric <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return numeric


def _safe_reason(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and value in SAFE_REASON_CODES:
        return value
    return "UNCLASSIFIED"


def _safe_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value[:limit]


def _safe_finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None


def diagnose_egress(
    status_file: str | Path,
    *,
    now: float | None = None,
    max_age_seconds: float = 30.0,
) -> tuple[dict[str, Any], int]:
    current = time.time() if now is None else now
    observation = read_egress_status(
        status_file,
        now=current,
        max_age_seconds=max_age_seconds,
    )

    egress_status = str(observation.get("status", "UNKNOWN"))
    connected = observation.get("connected") is True
    reason_code = _safe_reason(observation.get("reason_code"))
    action_code = ACTION_BY_REASON.get(reason_code or "", "CHECK_EGRESS_STATUS_SOURCE")
    incident_status = "UNAVAILABLE"
    exit_code = 3

    if egress_status == "CONNECTED":
        if connected:
            incident_status = "OK"
            action_code = "NONE"
            exit_code = 0
        else:
            reason_code = "STATUS_INCONSISTENT"
            action_code = "CHECK_EGRESS_STATUS_SOURCE"
    elif egress_status == "STARTING":
        incident_status = "ATTENTION"
        action_code = "WAIT_AND_OBSERVE"
        exit_code = 1
    elif egress_status == "RECONNECTING":
        incident_status = "RETRYING"
        if reason_code is None:
            action_code = "WAIT_AND_OBSERVE"
        elif reason_code == "UNCLASSIFIED":
            action_code = "CHECK_EGRESS_STATUS_SOURCE"
        exit_code = 1
    elif egress_status == "AUTH_FAILED":
        incident_status = "TERMINAL"
        if reason_code == "PUBLISH_CONFLICT":
            action_code = "CHECK_PUBLISH_CONFLICT"
        elif reason_code != "AUTH_FAILED":
            action_code = "CHECK_DESTINATION_CREDENTIAL"
        exit_code = 2
    elif egress_status == "FAILED":
        incident_status = "TERMINAL"
        if reason_code in {None, "UNCLASSIFIED"}:
            action_code = "CHECK_LOCAL_MEDIA_PATH"
        exit_code = 2
    elif egress_status == "STOPPED":
        incident_status = "ATTENTION"
        action_code = "CONFIRM_SESSION_DESIRED_STATE"
        exit_code = 1

    next_retry_in_seconds = None
    next_retry_at = _safe_finite(observation.get("next_retry_at"))
    if egress_status == "RECONNECTING" and next_retry_at is not None:
        next_retry_in_seconds = max(0.0, next_retry_at - current)

    observed_at = _safe_finite(observation.get("observed_at"))
    payload: dict[str, Any] = {
        "status": incident_status,
        "egress_status": egress_status,
        "reason_code": reason_code,
        "action_code": action_code,
        "attempt": max(0, int(observation.get("attempt", 0)))
        if isinstance(observation.get("attempt"), int)
        and not isinstance(observation.get("attempt"), bool)
        else 0,
        "next_retry_in_seconds": next_retry_in_seconds,
        "destination_scheme": _safe_text(observation.get("destination_scheme"), 20),
        "destination_host": _safe_text(observation.get("destination_host"), 253),
        "observed_at": observed_at,
    }
    return payload, exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="egress_failure_inspect_cli")
    parser.add_argument(
        "--status-file",
        default=os.getenv("NODE_EGRESS_STATUS_FILE", "/state/egress.json"),
        help="Redacted Egress Gateway status JSON",
    )
    parser.add_argument(
        "--max-age-seconds",
        type=_positive_finite,
        default=os.getenv("NODE_EGRESS_STATUS_MAX_AGE_SECONDS", "30"),
        help="Maximum age for non-terminal status (default: 30)",
    )
    args = parser.parse_args(argv)
    payload, exit_code = diagnose_egress(
        args.status_file,
        max_age_seconds=args.max_age_seconds,
    )
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
