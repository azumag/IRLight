"""Pure helpers for the Session <-> Node media-control heartbeat contract."""

from __future__ import annotations

import math
import uuid
from typing import Any


DESIRED_AUDIO_MODES = frozenset({"LIVE", "MUTED"})
ACTUAL_AUDIO_MODES = frozenset({"LIVE", "MUTED", "SILENT_FALLBACK"})


class MediaControlDeliveryError(RuntimeError):
    """Raised when media-control authority or an observation is malformed."""


def _timestamp(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MediaControlDeliveryError(f"invalid {label}")
    try:
        parsed = float(value)
    except (OverflowError, TypeError, ValueError):
        raise MediaControlDeliveryError(f"invalid {label}") from None
    if not math.isfinite(parsed) or parsed < 0:
        raise MediaControlDeliveryError(f"invalid {label}")
    return parsed


def _version(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MediaControlDeliveryError(f"invalid {label}")
    return value


def _command_id(value: object, *, version: int, label: str) -> str | None:
    if value is None:
        if version == 0:
            return None
        raise MediaControlDeliveryError(f"invalid {label}")
    if not isinstance(value, str):
        raise MediaControlDeliveryError(f"invalid {label}")
    try:
        uuid.UUID(value)
    except ValueError:
        raise MediaControlDeliveryError(f"invalid {label}") from None
    return value


def _idempotency_key(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 200:
        raise MediaControlDeliveryError("invalid audio idempotency key")
    return value


def session_audio_command(session: object) -> dict[str, object]:
    """Return the current versioned final-state command for Node delivery.

    Legacy Sessions without ``media_control`` remain compatible at version 0;
    every Session created by the current contract carries the explicit state.
    """

    if not isinstance(session, dict):
        raise MediaControlDeliveryError("invalid Session authority")
    media_control = session.get("media_control")
    if media_control is None:
        updated_at = _timestamp(session.get("updated_at"), "Session update time")
        return {
            "desired_mode": "LIVE",
            "version": 0,
            "command_id": None,
            "idempotency_key": None,
            "updated_at": updated_at,
        }
    if not isinstance(media_control, dict) or not isinstance(
        media_control.get("audio"), dict
    ):
        raise MediaControlDeliveryError("invalid Session media-control authority")
    audio: dict[str, Any] = media_control["audio"]
    desired_mode = audio.get("desired_mode")
    if not isinstance(desired_mode, str) or desired_mode not in DESIRED_AUDIO_MODES:
        raise MediaControlDeliveryError("invalid Session desired audio mode")
    version = _version(audio.get("version"), "Session audio version")
    return {
        "desired_mode": desired_mode,
        "version": version,
        "command_id": _command_id(
            audio.get("command_id"), version=version, label="Session audio command id"
        ),
        "idempotency_key": _idempotency_key(audio.get("idempotency_key")),
        "updated_at": _timestamp(audio.get("updated_at"), "Session audio update time"),
    }


def normalize_audio_observation(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise MediaControlDeliveryError("invalid audio observation")
    actual_mode = value.get("actual_mode")
    if not isinstance(actual_mode, str) or actual_mode not in ACTUAL_AUDIO_MODES:
        raise MediaControlDeliveryError("invalid observed audio mode")
    version = _version(value.get("control_version"), "observed audio version")
    return {
        "actual_mode": actual_mode,
        "control_version": version,
        "command_id": _command_id(
            value.get("command_id"), version=version, label="observed audio command id"
        ),
        "observed_at": _timestamp(value.get("observed_at"), "audio observation time"),
    }


def classify_audio_observation(session: object, observation: object) -> str:
    """Classify an ACK without allowing it to rewrite command authority.

    The caller may mutate ``actual_mode`` only for ``matched``.  Older,
    future, or same-version/different-command observations are deliberately
    ignored so delayed heartbeats cannot roll Session state backward.
    """

    command = session_audio_command(session)
    observed = normalize_audio_observation(observation)
    current_version = int(command["version"])
    observed_version = int(observed["control_version"])
    if observed_version < current_version:
        return "stale"
    if observed_version > current_version:
        return "future"
    if observed["command_id"] != command["command_id"]:
        return "command_mismatch"
    return "matched"
