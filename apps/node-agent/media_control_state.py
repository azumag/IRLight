"""Node-local media-control command reconciliation.

The Control Plane owns the desired per-Session audio command while Continuity
owns the actual media-pipeline state.  This module keeps that boundary small:
versioned heartbeat commands are reconciled into ``control.json`` and the
matching Continuity ``status.json`` observation is converted into a safe ACK
payload.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any


DESIRED_AUDIO_MODES = frozenset({"LIVE", "MUTED"})
ACTUAL_AUDIO_MODES = frozenset({"LIVE", "MUTED", "SILENT_FALLBACK"})


class MediaControlStateError(RuntimeError):
    """Raised when node-local media-control authority cannot be trusted."""


class MediaControlStateConflict(MediaControlStateError):
    """Raised when one version names two different commands/final states."""


def _finite_timestamp(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MediaControlStateError(f"invalid {label}")
    try:
        parsed = float(value)
    except (OverflowError, TypeError, ValueError):
        raise MediaControlStateError(f"invalid {label}") from None
    if not math.isfinite(parsed) or parsed < 0:
        raise MediaControlStateError(f"invalid {label}")
    return parsed


def _version(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MediaControlStateError(f"invalid {label}")
    return value


def _command_id(value: object, *, version: int, label: str) -> str | None:
    if value is None:
        if version == 0:
            return None
        raise MediaControlStateError(f"invalid {label}")
    if not isinstance(value, str):
        raise MediaControlStateError(f"invalid {label}")
    try:
        uuid.UUID(value)
    except ValueError:
        raise MediaControlStateError(f"invalid {label}") from None
    return value


def _optional_idempotency_key(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 200:
        raise MediaControlStateError("invalid audio idempotency key")
    return value


def _load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle, parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)))
    except FileNotFoundError:
        raise
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        raise MediaControlStateError("cannot read media-control state") from exc
    if not isinstance(value, dict):
        raise MediaControlStateError("invalid media-control state payload")
    return value


def _atomic_write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            try:
                json.dump(
                    value,
                    handle,
                    ensure_ascii=False,
                    sort_keys=True,
                    allow_nan=False,
                )
            except (TypeError, ValueError) as exc:
                raise MediaControlStateError("media-control state cannot be serialized") from exc
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def normalize_remote_audio_command(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise MediaControlStateError("invalid remote audio command")
    desired_mode = value.get("desired_mode")
    if not isinstance(desired_mode, str) or desired_mode not in DESIRED_AUDIO_MODES:
        raise MediaControlStateError("invalid remote audio mode")
    version = _version(value.get("version"), "remote audio version")
    command_id = _command_id(
        value.get("command_id"), version=version, label="remote audio command id"
    )
    idempotency_key = _optional_idempotency_key(value.get("idempotency_key"))
    updated_at = _finite_timestamp(value.get("updated_at"), "remote audio update time")
    return {
        "audio_mode": desired_mode,
        "version": version,
        "command_id": command_id,
        "idempotency_key": idempotency_key,
        "updated_at": updated_at,
    }


def normalize_local_control_state(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise MediaControlStateError("invalid local audio control state")
    mode = value.get("audio_mode")
    if not isinstance(mode, str) or mode not in DESIRED_AUDIO_MODES:
        raise MediaControlStateError("invalid local audio mode")
    version = _version(value.get("version"), "local audio version")
    command_id = _command_id(
        value.get("command_id"), version=version, label="local audio command id"
    )
    return {
        "audio_mode": mode,
        "version": version,
        "command_id": command_id,
        "idempotency_key": _optional_idempotency_key(value.get("idempotency_key")),
        "updated_at": _finite_timestamp(value.get("updated_at"), "local audio update time"),
    }


def reconcile_audio_command(path: Path, remote: object) -> str:
    """Reconcile one heartbeat command into Continuity's control authority.

    Returns ``applied``, ``noop`` or ``stale``.  A same-version disagreement is
    never guessed through: it is an authority conflict and leaves the existing
    file untouched.
    """

    desired = normalize_remote_audio_command(remote)
    try:
        current = normalize_local_control_state(_load_json(path))
    except FileNotFoundError:
        _atomic_write_json(path, desired)
        return "applied"

    remote_version = int(desired["version"])
    local_version = int(current["version"])
    if remote_version < local_version:
        return "stale"
    if remote_version == local_version:
        if (
            desired["audio_mode"] == current["audio_mode"]
            and desired["command_id"] == current["command_id"]
        ):
            return "noop"
        raise MediaControlStateConflict("same audio version has conflicting authority")

    _atomic_write_json(path, desired)
    return "applied"


def read_audio_observation(path: Path) -> dict[str, object] | None:
    """Read Continuity's current audio observation for the heartbeat ACK."""

    try:
        value = _load_json(path)
    except FileNotFoundError:
        return None

    actual_mode = value.get("actual_audio_mode")
    if not isinstance(actual_mode, str) or actual_mode not in ACTUAL_AUDIO_MODES:
        raise MediaControlStateError("invalid actual audio mode")
    version = _version(value.get("control_version"), "observed audio version")
    command_id = _command_id(
        value.get("command_id"), version=version, label="observed audio command id"
    )
    observed_at = _finite_timestamp(value.get("updated_at"), "audio observation time")
    return {
        "actual_mode": actual_mode,
        "control_version": version,
        "command_id": command_id,
        "observed_at": observed_at,
    }
