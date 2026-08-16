from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from threading import RLock


class AudioMode(StrEnum):
    LIVE = "LIVE"
    MUTED = "MUTED"


class VideoSource(StrEnum):
    LIVE = "LIVE"
    STANDBY = "STANDBY"


class ActualAudio(StrEnum):
    LIVE = "LIVE"
    MUTED = "MUTED"
    SILENT_FALLBACK = "SILENT_FALLBACK"


class SessionStatus(StrEnum):
    HOLDING = "HOLDING"
    STABILIZING = "STABILIZING"
    LIVE = "LIVE"


@dataclass(frozen=True)
class Decision:
    session_status: SessionStatus
    video_source: VideoSource
    actual_audio: ActualAudio
    video_recent: bool
    audio_recent: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class ContinuityState:
    """Pure decision model shared by the pipeline and unit tests.

    Buffer probes call observe_*(). A periodic reconciliation loop calls decide().
    The desired audio mode is orthogonal to input health, so MUTED survives a
    disconnect/reconnect cycle.
    """

    def __init__(self, *, input_timeout: float, stable_window: float) -> None:
        if input_timeout <= 0:
            raise ValueError("input_timeout must be positive")
        if stable_window < 0:
            raise ValueError("stable_window must be non-negative")

        self.input_timeout = input_timeout
        self.stable_window = stable_window
        self.desired_audio = AudioMode.LIVE
        self._last_video_at: float | None = None
        self._last_audio_at: float | None = None
        self._video_stable_since: float | None = None
        self._lock = RLock()

    def set_audio_mode(self, mode: AudioMode | str) -> None:
        with self._lock:
            self.desired_audio = AudioMode(mode)

    def observe_video(self, now: float) -> None:
        with self._lock:
            if (
                self._last_video_at is None
                or now - self._last_video_at > self.input_timeout
            ):
                self._video_stable_since = now
            self._last_video_at = now

    def observe_audio(self, now: float) -> None:
        with self._lock:
            self._last_audio_at = now

    def decide(self, now: float) -> Decision:
        with self._lock:
            video_recent = (
                self._last_video_at is not None
                and now - self._last_video_at <= self.input_timeout
            )
            audio_recent = (
                self._last_audio_at is not None
                and now - self._last_audio_at <= self.input_timeout
            )

            if not video_recent:
                self._video_stable_since = None
                video_source = VideoSource.STANDBY
                status = SessionStatus.HOLDING
            else:
                stable_since = self._video_stable_since
                stable = (
                    stable_since is not None
                    and now - stable_since >= self.stable_window
                )
                if stable:
                    video_source = VideoSource.LIVE
                    status = SessionStatus.LIVE
                else:
                    video_source = VideoSource.STANDBY
                    status = SessionStatus.STABILIZING

            if self.desired_audio is AudioMode.MUTED:
                actual_audio = ActualAudio.MUTED
            elif video_source is VideoSource.LIVE and audio_recent:
                actual_audio = ActualAudio.LIVE
            else:
                actual_audio = ActualAudio.SILENT_FALLBACK

            return Decision(
                session_status=status,
                video_source=video_source,
                actual_audio=actual_audio,
                video_recent=video_recent,
                audio_recent=audio_recent,
            )
