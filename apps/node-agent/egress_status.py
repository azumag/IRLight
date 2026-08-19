from __future__ import annotations

import json
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


def read_egress_status(path: str | Path) -> dict[str, Any]:
    status_path = Path(path)
    try:
        raw = json.loads(status_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {
            "status": "UNKNOWN",
            "connected": False,
            "attempt": 0,
            "reason_code": "STATUS_UNAVAILABLE",
            "rendered_buffers": 0,
            "next_retry_at": None,
            "destination_scheme": None,
            "destination_host": None,
            "observed_at": time.time(),
        }
    if not isinstance(raw, dict):
        return {
            "status": "UNKNOWN",
            "connected": False,
            "attempt": 0,
            "reason_code": "STATUS_INVALID",
            "rendered_buffers": 0,
            "next_retry_at": None,
            "destination_scheme": None,
            "destination_host": None,
            "observed_at": time.time(),
        }
    status = str(raw.get("status", "UNKNOWN"))
    if status not in ALLOWED_STATUSES:
        status = "UNKNOWN"
    return {
        "status": status,
        "connected": status == "CONNECTED" and bool(raw.get("connected", False)),
        "attempt": max(0, int(raw.get("attempt", 0) or 0)),
        "reason_code": str(raw.get("reason_code"))[:100] if raw.get("reason_code") else None,
        "rendered_buffers": max(0, int(raw.get("rendered_buffers", 0) or 0)),
        "next_retry_at": raw.get("next_retry_at") if isinstance(raw.get("next_retry_at"), (int, float)) else None,
        "destination_scheme": str(raw.get("destination_scheme"))[:20] if raw.get("destination_scheme") else None,
        "destination_host": str(raw.get("destination_host"))[:253] if raw.get("destination_host") else None,
        "observed_at": float(raw.get("observed_at", time.time())),
    }
