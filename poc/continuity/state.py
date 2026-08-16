from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from threading import RLock
from typing import Any


class AudioMode(str, Enum):
    LIVE = "LIVE"
    MUTED = "MUTED"


class AudioActual(str, Enum):
    LIVE = "LIVE"
    MUTED = "MUTED"
    APPLYING = "APPLYING"
    FAILED = "FAILED"


class VersionConflictError(ValueError):
    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(f"media control version conflict: expected={expected}, actual={actual}")
        self.expected = expected
        self.actual = actual


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RuntimeState:
    """Thread-safe in-memory state for the single-session Phase 0 PoC.

    The production design will persist desired state in the Control Plane and
    reconcile it on Media Nodes. Keeping desired and actual state separate here
    lets the PoC exercise the same semantics without a database.
    """

    session_status: str = "READY"
    display_source: str = "STANDBY"
    input_connected: bool = False
    input_has_video: bool = False
    input_has_audio: bool = False
    output_connected: bool = False
    audio_desired: AudioMode = AudioMode.LIVE
    audio_actual: AudioActual = AudioActual.MUTED
    media_control_version: int = 0
    last_error: str | None = None
    last_audio_reason: str | None = "INPUT_UNAVAILABLE"
    started_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    last_video_at: str | None = None
    last_audio_at: str | None = None
    _lock: RLock = field(default_factory=RLock, repr=False, compare=False)

    def set_audio_desired(
        self,
        mode: AudioMode,
        *,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if expected_version is not None and expected_version != self.media_control_version:
                raise VersionConflictError(expected_version, self.media_control_version)

            # PUT semantics: sending the already-desired state is idempotent.
            if self.audio_desired == mode:
                return self._snapshot_unlocked()

            self.audio_desired = mode
            self.audio_actual = AudioActual.APPLYING
            self.last_audio_reason = None
            self.media_control_version += 1
            self.updated_at = utc_now_iso()
            return self._snapshot_unlocked()

    def mark_audio_actual(self, mode: AudioActual, reason: str | None = None) -> None:
        with self._lock:
            self.audio_actual = mode
            self.last_audio_reason = reason
            self.updated_at = utc_now_iso()

    def mark_input(
        self,
        *,
        connected: bool,
        has_video: bool,
        has_audio: bool,
        session_status: str,
        display_source: str,
        last_video_at: str | None = None,
        last_audio_at: str | None = None,
    ) -> None:
        with self._lock:
            self.input_connected = connected
            self.input_has_video = has_video
            self.input_has_audio = has_audio
            self.session_status = session_status
            self.display_source = display_source
            if last_video_at is not None:
                self.last_video_at = last_video_at
            if last_audio_at is not None:
                self.last_audio_at = last_audio_at
            self.updated_at = utc_now_iso()

    def mark_output(self, connected: bool, error: str | None = None) -> None:
        with self._lock:
            self.output_connected = connected
            if error is not None:
                self.last_error = error
            elif connected:
                self.last_error = None
            self.updated_at = utc_now_iso()

    def set_error(self, error: str | None) -> None:
        with self._lock:
            self.last_error = error
            self.updated_at = utc_now_iso()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_unlocked()

    def _snapshot_unlocked(self) -> dict[str, Any]:
        return {
            "session_status": self.session_status,
            "display_source": self.display_source,
            "input_connected": self.input_connected,
            "input_has_video": self.input_has_video,
            "input_has_audio": self.input_has_audio,
            "output_connected": self.output_connected,
            "audio": {
                "desired": self.audio_desired.value,
                "actual": self.audio_actual.value,
                "reason": self.last_audio_reason,
                "version": self.media_control_version,
            },
            "last_error": self.last_error,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "last_video_at": self.last_video_at,
            "last_audio_at": self.last_audio_at,
        }
