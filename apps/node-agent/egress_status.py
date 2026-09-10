from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from typing import Any


ALLOWED_STATUSES = {
    "STARTING",
    "CONNECTED",
    "RECONNECTING",
    "AUTH_FAILED",
    "FAILED",
    "STOPPED",
}
TERMINAL_STATUSES = {"AUTH_FAILED", "FAILED", "STOPPED"}


def _unknown(reason_code: str) -> dict[str, Any]:
    return {
        "status": "UNKNOWN",
        "connected": False,
        "attempt": 0,
        "reason_code": reason_code,
        "rendered_buffers": 0,
        "next_retry_at": None,
        "destination_scheme": None,
        "destination_host": None,
        "observed_at": time.time(),
    }


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is not allowed: {value}")


def _reject_duplicate_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key is not allowed: {key}")
        result[key] = value
    return result


def _nonnegative_int(value: object, default: int = 0) -> int:
    try:
        return max(0, int(value))  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return default


def _nonnegative_finite_number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(numeric) or numeric < 0:
        return None
    return value


def read_egress_status(
    path: str | Path,
    *,
    now: float | None = None,
    max_age_seconds: float | None = None,
) -> dict[str, Any]:
    status_path = Path(path)
    try:
        raw = json.loads(
            status_path.read_text(encoding="utf-8"),
            parse_constant=_reject_json_constant,
            object_pairs_hook=_reject_duplicate_object_pairs,
        )
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return _unknown("STATUS_UNAVAILABLE")
    if not isinstance(raw, dict):
        return _unknown("STATUS_INVALID")

    status = str(raw.get("status", "UNKNOWN"))
    if status not in ALLOWED_STATUSES:
        return _unknown("STATUS_INVALID")

    current = time.time() if now is None else now
    raw_observed_at = raw.get("observed_at")
    if raw_observed_at is None:
        observed_at = current
    else:
        try:
            observed_at = float(raw_observed_at)
        except (TypeError, ValueError, OverflowError):
            return _unknown("STATUS_INVALID")
        if not math.isfinite(observed_at) or observed_at < 0:
            return _unknown("STATUS_INVALID")
    if max_age_seconds is None:
        try:
            max_age_seconds = float(
                os.getenv("NODE_EGRESS_STATUS_MAX_AGE_SECONDS", "30")
            )
        except ValueError:
            max_age_seconds = 30.0
    if (
        status not in TERMINAL_STATUSES
        and max_age_seconds > 0
        and current - observed_at > max_age_seconds
    ):
        result = _unknown("STATUS_STALE")
        result["observed_at"] = observed_at
        return result

    return {
        "status": status,
        "connected": status == "CONNECTED" and bool(raw.get("connected", False)),
        "attempt": _nonnegative_int(raw.get("attempt", 0)),
        "reason_code": (
            str(raw.get("reason_code"))[:100] if raw.get("reason_code") else None
        ),
        "rendered_buffers": _nonnegative_int(raw.get("rendered_buffers", 0)),
        "next_retry_at": _nonnegative_finite_number(raw.get("next_retry_at")),
        "destination_scheme": (
            str(raw.get("destination_scheme"))[:20]
            if raw.get("destination_scheme")
            else None
        ),
        "destination_host": (
            str(raw.get("destination_host"))[:253]
            if raw.get("destination_host")
            else None
        ),
        "observed_at": observed_at,
    }
