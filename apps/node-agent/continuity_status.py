from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


ALLOWED_SESSION_STATUSES = {"HOLDING", "STABILIZING", "LIVE"}
ALLOWED_VIDEO_SOURCES = {"STANDBY", "LIVE"}
ALLOWED_AUDIO_MODES = {"LIVE", "MUTED", "SILENT_FALLBACK"}


def _unknown(reason_code: str) -> dict[str, Any]:
    return {
        "session_status": "UNKNOWN",
        "video_source": "UNKNOWN",
        "desired_audio_mode": None,
        "actual_audio_mode": None,
        "input_video_recent": False,
        "input_audio_recent": False,
        "started_at": None,
        "observed_at": time.time(),
        "reason_code": reason_code,
    }


def _float(value: object, default: float) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return default


def read_continuity_status(
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

    session_status = str(raw.get("session_status", "UNKNOWN"))
    video_source = str(raw.get("video_source", "UNKNOWN"))
    desired_audio = str(raw.get("desired_audio_mode", "")) or None
    actual_audio = str(raw.get("actual_audio_mode", "")) or None
    if session_status not in ALLOWED_SESSION_STATUSES or video_source not in ALLOWED_VIDEO_SOURCES:
        return _unknown("STATUS_INVALID")
    if desired_audio not in {"LIVE", "MUTED"} or actual_audio not in ALLOWED_AUDIO_MODES:
        return _unknown("STATUS_INVALID")

    current = time.time() if now is None else now
    observed_at = _float(raw.get("updated_at"), current)
    if max_age_seconds is None:
        try:
            max_age_seconds = float(os.getenv("NODE_CONTINUITY_STATUS_MAX_AGE_SECONDS", "30"))
        except ValueError:
            max_age_seconds = 30.0
    if max_age_seconds > 0 and current - observed_at > max_age_seconds:
        result = _unknown("STATUS_STALE")
        result["observed_at"] = observed_at
        return result

    started_at_raw = raw.get("started_at")
    started_at = (
        _float(started_at_raw, current)
        if isinstance(started_at_raw, (int, float))
        else None
    )
    return {
        "session_status": session_status,
        "video_source": video_source,
        "desired_audio_mode": desired_audio,
        "actual_audio_mode": actual_audio,
        "input_video_recent": bool(raw.get("input_video_recent", False)),
        "input_audio_recent": bool(raw.get("input_audio_recent", False)),
        "started_at": started_at,
        "observed_at": observed_at,
        "reason_code": None,
    }
