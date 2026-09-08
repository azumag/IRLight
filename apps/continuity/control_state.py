"""Fail-safe reader for the Control Plane's audio command authority."""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ControlCommandState:
    audio_mode: str
    version: int
    command_id: str | None


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate control key: {key}")
        value[key] = item
    return value


def _reject_non_finite_constant(value: str) -> None:
    raise ValueError(f"non-finite control number is not allowed: {value}")


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
        idempotency_key = value.get("idempotency_key")
        updated_at = value.get("updated_at")
        if mode not in {"LIVE", "MUTED"}:
            raise ValueError("invalid audio mode")
        if isinstance(version, bool) or not isinstance(version, int) or version < 0:
            raise ValueError("invalid control version")
        if command_id is not None:
            if not isinstance(command_id, str):
                raise ValueError("invalid command id")
            try:
                uuid.UUID(command_id)
            except ValueError as exc:
                raise ValueError("invalid command id") from exc
        if idempotency_key is not None and (
            not isinstance(idempotency_key, str) or len(idempotency_key) > 200
        ):
            raise ValueError("invalid idempotency key")
        if (
            isinstance(updated_at, bool)
            or not isinstance(updated_at, (int, float))
        ):
            raise ValueError("invalid control update time")
        try:
            normalized_updated_at = float(updated_at)
        except (OverflowError, ValueError):
            raise ValueError("invalid control update time") from None
        if not math.isfinite(normalized_updated_at):
            raise ValueError("invalid control update time")
        return ControlCommandState(mode, version, command_id)

    def read(self) -> tuple[ControlCommandState, str | None]:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                current = self._validate(
                    json.load(
                        handle,
                        object_pairs_hook=_reject_duplicate_keys,
                        parse_constant=_reject_non_finite_constant,
                    )
                )
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
