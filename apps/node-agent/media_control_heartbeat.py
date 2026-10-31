"""Heartbeat adapter for per-Session media-control delivery.

Keep the transport-specific shape outside ``agent.py``: the Agent can pass its
normal heartbeat payload/response through these helpers while
``media_control_state`` owns validation and local authority reconciliation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from media_control_state import (
    MediaControlStateError,
    read_audio_observation,
    reconcile_audio_command,
)


def add_audio_observation(
    payload: dict[str, object], *, status_path: Path | None
) -> dict[str, object]:
    """Attach a safe Continuity audio observation to one heartbeat request.

    Missing status is normal during startup and therefore leaves the request
    unchanged. Malformed status is deliberately surfaced to the caller instead
    of emitting an ACK that the Control Plane could mistake for applied state.
    """

    if status_path is None:
        return payload
    observation = read_audio_observation(status_path)
    if observation is None:
        return payload
    enriched = dict(payload)
    enriched["media_control"] = {"audio": observation}
    return enriched


def apply_audio_command(
    response: dict[str, object], *, control_path: Path | None
) -> str | None:
    """Apply the versioned audio command returned by a heartbeat.

    ``None`` means no command was supplied (mixed-version compatibility).
    Otherwise returns the reconcile result from ``media_control_state``.
    """

    if control_path is None:
        return None
    media_control = response.get("media_control")
    if media_control is None:
        return None
    if not isinstance(media_control, dict):
        raise MediaControlStateError("invalid heartbeat media-control payload")
    audio: Any = media_control.get("audio")
    if audio is None:
        return None
    return reconcile_audio_command(control_path, audio)


def bootstrap_audio_command(response: dict[str, object]) -> dict[str, object] | None:
    """Return bootstrap audio authority in heartbeat-command shape.

    New Control Planes may send the nested media-control contract directly.
    During rolling upgrades, preserve the existing flat bootstrap fields and
    translate them into the same shape.  A completely legacy response without
    audio fields keeps the historical Node-side bootstrap path untouched.
    """

    nested = response.get("media_control")
    if nested is not None:
        if not isinstance(nested, dict):
            raise MediaControlStateError("invalid bootstrap media-control payload")
        audio = nested.get("audio")
        if audio is None:
            return None
        if not isinstance(audio, dict):
            raise MediaControlStateError("invalid bootstrap audio command")
        return dict(audio)

    known = {
        "audio_mode",
        "audio_version",
        "audio_command_id",
        "audio_idempotency_key",
        "audio_updated_at",
    }
    if not any(field in response for field in known):
        return None
    return {
        "desired_mode": response.get("audio_mode", "LIVE"),
        "version": response.get("audio_version", 0),
        "command_id": response.get("audio_command_id"),
        "idempotency_key": response.get("audio_idempotency_key"),
        "updated_at": response.get("audio_updated_at"),
    }
