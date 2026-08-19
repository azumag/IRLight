from __future__ import annotations

import json
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


def _nonnegative_int(value: object, default: int = 0) -> int:
    try:
        return max(0, int(value))  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return default


def _float(value: object, default: float) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return default


def read_egress_status(
    path: str | Path,
    *,
    now: float | None = None,
    max_age_seconds: float | None = None,
) -> dict[str, Any]:
    status_path = Path(path)
    try:
        raw = json.loads(status_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return _unknown("STATUS_UNAVAILABLE")
    if not isinstance(raw, dict):
        return _unknown("STATUS_INVALID")

    status = str(raw.get("status", "UNKNOWN"))
    if status not in ALLOWED_STATUSES:
        return _unknown("STATUS_INVALID")

    current = time.time() if now is None else now
    observed_at = _float(raw.get("observed_at"), current)
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
        "next_retry_at": (
            raw.get("next_retry_at")
            if isinstance(raw.get("next_retry_at"), (int, float))
            else None
        ),
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
