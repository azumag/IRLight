"""Fail-safe reader for the Control Plane's audio command authority."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ControlCommandState:
    audio_mode: str
    version: int
    command_id: str | None


class ControlStateReader:
    """Preserve the last valid command; start MUTED if no authority is readable."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._last_valid: ControlCommandState | None = None

    @staticmethod
    def _validate(value: Any) -> ControlCommandState:
        if not isinstance(value, dict):
            raise ValueError("invalid control structure")
        mode = value.get("audio_mode")
        version = value.get("version")
        command_id = value.get("command_id")
        updated_at = value.get("updated_at")
        if mode not in {"LIVE", "MUTED"}:
            raise ValueError("invalid audio mode")
        if isinstance(version, bool) or not isinstance(version, int) or version < 0:
            raise ValueError("invalid control version")
        if command_id is not None and not isinstance(command_id, str):
            raise ValueError("invalid command id")
        if (
            isinstance(updated_at, bool)
            or not isinstance(updated_at, (int, float))
            or not math.isfinite(float(updated_at))
        ):
            raise ValueError("invalid control update time")
        return ControlCommandState(mode, version, command_id)

    def read(self) -> tuple[ControlCommandState, str | None]:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                current = self._validate(json.load(handle))
        except FileNotFoundError:
            return self._fallback(), "CONTROL_STATE_UNAVAILABLE"
        except (json.JSONDecodeError, OSError, ValueError):
            return self._fallback(), "CONTROL_STATE_INVALID"
        self._last_valid = current
        return current, None

    def _fallback(self) -> ControlCommandState:
        if self._last_valid is not None:
            return self._last_valid
        return ControlCommandState("MUTED", 0, None)
