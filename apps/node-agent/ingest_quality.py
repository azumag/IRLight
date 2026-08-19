"""Sample live ingest media and classify quality degradation.

This complements ``ingest_policy.py``. MediaMTX's path API provides codec,
resolution and byte counters, while ffprobe samples the actual RTSP media to
observe frame cadence, keyframes and timestamp progression.

DEGRADED is intentionally non-destructive: unlike hard format-policy failures,
quality failures do not kick the publisher. Continuity/Control Plane can keep
serving the stream while surfacing the reason and deciding whether to enter
HOLDING later.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class IngestQualityConfig:
    input_url: str = "rtsp://mediamtx:8554/live/input"
    sample_seconds: float = 4.0
    min_video_fps: float = 20.0
    preferred_min_fps: float = 24.0
    preferred_max_fps: float = 35.0
    max_video_fps: float = 40.0
    max_gop_seconds: float = 4.0
    min_progress_ratio: float = 0.60
    min_bitrate_bps: int = 500_000
    low_bitrate_samples: int = 2
    ffprobe_path: str = "ffprobe"
    timeout_margin_seconds: float = 3.0

    @classmethod
    def from_env(cls) -> "IngestQualityConfig":
        def env_float(name: str, default: float, minimum: float) -> float:
            try:
                return max(minimum, float(os.getenv(name, str(default))))
            except ValueError:
                return default

        def env_int(name: str, default: int, minimum: int) -> int:
            try:
                return max(minimum, int(os.getenv(name, str(default))))
            except ValueError:
                return default

        sample_seconds = env_float("NODE_INGEST_SAMPLE_SECONDS", 4.0, 1.0)
        preferred_min = env_float("NODE_INGEST_PREFERRED_MIN_FPS", 24.0, 1.0)
        preferred_max = env_float("NODE_INGEST_PREFERRED_MAX_FPS", 35.0, preferred_min)
        return cls(
            input_url=os.getenv(
                "NODE_INGEST_SAMPLE_URL", "rtsp://mediamtx:8554/live/input"
            ),
            sample_seconds=min(sample_seconds, 10.0),
            min_video_fps=env_float("NODE_INGEST_MIN_FPS", 20.0, 1.0),
            preferred_min_fps=preferred_min,
            preferred_max_fps=preferred_max,
            max_video_fps=env_float("NODE_INGEST_MAX_FPS", 40.0, preferred_max),
            max_gop_seconds=env_float("NODE_INGEST_MAX_GOP_SECONDS", 4.0, 0.5),
            min_progress_ratio=min(
                env_float("NODE_INGEST_MIN_PROGRESS_RATIO", 0.60, 0.1), 1.0
            ),
            min_bitrate_bps=env_int("NODE_INGEST_MIN_BITRATE_BPS", 500_000, 0),
            low_bitrate_samples=env_int("NODE_INGEST_LOW_BITRATE_SAMPLES", 2, 1),
            ffprobe_path=os.getenv("NODE_FFPROBE_PATH", "ffprobe"),
            timeout_margin_seconds=env_float(
                "NODE_INGEST_SAMPLE_TIMEOUT_MARGIN_SECONDS", 3.0, 0.5
            ),
        )


RunFn = Callable[..., subprocess.CompletedProcess[str]]


class IngestQualitySampler:
    def __init__(
        self,
        config: IngestQualityConfig | None = None,
        *,
        runner: RunFn = subprocess.run,
    ) -> None:
        self.config = config or IngestQualityConfig.from_env()
        self._runner = runner
        self._last_source_id: str | None = None
        self._low_bitrate_samples = 0

    def _reset_source(self, source_id: str | None) -> None:
        if source_id != self._last_source_id:
            self._last_source_id = source_id
            self._low_bitrate_samples = 0

    def sample(self) -> dict[str, Any]:
        command = [
            self.config.ffprobe_path,
            "-v",
            "error",
            "-rtsp_transport",
            "tcp",
            "-read_intervals",
            f"%+{self.config.sample_seconds:g}",
            "-show_frames",
            "-show_entries",
            "frame=media_type,key_frame,best_effort_timestamp_time,pkt_dts_time",
            "-of",
            "compact=p=0:nk=0",
            self.config.input_url,
        ]
        started = time.monotonic()
        try:
            result = self._runner(
                command,
                capture_output=True,
                text=True,
                timeout=self.config.sample_seconds + self.config.timeout_margin_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            elapsed = time.monotonic() - started
            # A single stalled track can keep ffprobe waiting past read_intervals even
            # while the other track continues to produce frames. Preserve the compact
            # line-oriented output captured before timeout so VIDEO_TIMEOUT and
            # AUDIO_TIMEOUT remain distinguishable instead of collapsing everything
            # into MEDIA_SAMPLE_TIMEOUT.
            partial = _decode_subprocess_output(exc.stdout)
            payload = _parse_frame_output(partial)
            if payload.get("frames"):
                evaluated = evaluate_frame_sample(
                    payload,
                    self.config,
                    sample_elapsed_seconds=elapsed,
                )
                if not evaluated.get("reasons"):
                    evaluated["reasons"] = ["MEDIA_SAMPLE_TIMEOUT"]
                return evaluated
            return {
                "sample_elapsed_seconds": round(elapsed, 3),
                "video_frames": 0,
                "audio_frames": 0,
                "video_fps": None,
                "video_timestamp_span_seconds": None,
                "audio_timestamp_span_seconds": None,
                "keyframes": 0,
                "max_gop_seconds": None,
                "reasons": ["MEDIA_SAMPLE_TIMEOUT"],
                "warnings": [],
            }

        elapsed = time.monotonic() - started
        if result.returncode != 0:
            detail = (result.stderr or "ffprobe failed").strip().replace("\n", " ")[:160]
            return {
                "sample_elapsed_seconds": round(elapsed, 3),
                "video_frames": 0,
                "audio_frames": 0,
                "video_fps": None,
                "video_timestamp_span_seconds": None,
                "audio_timestamp_span_seconds": None,
                "keyframes": 0,
                "max_gop_seconds": None,
                "reasons": ["MEDIA_SAMPLE_FAILED"],
                "warnings": [],
                "error": detail,
            }
        payload = _parse_frame_output(result.stdout or "")
        if payload.get("invalid"):
            return {
                "sample_elapsed_seconds": round(elapsed, 3),
                "video_frames": 0,
                "audio_frames": 0,
                "video_fps": None,
                "video_timestamp_span_seconds": None,
                "audio_timestamp_span_seconds": None,
                "keyframes": 0,
                "max_gop_seconds": None,
                "reasons": ["MEDIA_SAMPLE_INVALID_JSON"],
                "warnings": [],
            }
        return evaluate_frame_sample(
            payload,
            self.config,
            sample_elapsed_seconds=elapsed,
        )

    def augment(self, observation: dict[str, Any]) -> dict[str, Any]:
        """Merge live quality results into a MediaMTX policy observation."""
        result = dict(observation)
        source_id_raw = result.get("source_id")
        source_id = source_id_raw if isinstance(source_id_raw, str) else None
        self._reset_source(source_id)

        if not result.get("online") or result.get("status") in {
            "OFFLINE",
            "UNKNOWN",
            "REJECTED",
        }:
            if not result.get("online"):
                self._low_bitrate_samples = 0
            return result

        quality = self.sample()
        reasons = list(result.get("reasons", []))
        warnings = list(result.get("warnings", []))
        reasons.extend(str(value) for value in quality.get("reasons", []))
        warnings.extend(str(value) for value in quality.get("warnings", []))

        bitrate = _safe_float(result.get("bitrate_bps"))
        if bitrate is not None and self.config.min_bitrate_bps > 0:
            if bitrate < self.config.min_bitrate_bps:
                self._low_bitrate_samples += 1
                if self._low_bitrate_samples >= self.config.low_bitrate_samples:
                    reasons.append("BITRATE_TOO_LOW")
                else:
                    warnings.append("BITRATE_BELOW_MIN_PENDING")
            else:
                self._low_bitrate_samples = 0

        result["quality"] = {
            key: value
            for key, value in quality.items()
            if key not in {"reasons", "warnings"}
        }
        result["reasons"] = _dedupe(reasons)
        result["warnings"] = _dedupe(warnings)

        if result["reasons"]:
            # Hard format-policy failures have already been classified REJECTED
            # and returned above. Quality reasons are non-destructive DEGRADED.
            result["status"] = "DEGRADED"
        elif result["warnings"] and result.get("status") == "ACCEPTED":
            result["status"] = "WARNING"
        return result


def evaluate_frame_sample(
    payload: dict[str, Any],
    config: IngestQualityConfig,
    *,
    sample_elapsed_seconds: float | None = None,
) -> dict[str, Any]:
    frames = payload.get("frames", [])
    frames = frames if isinstance(frames, list) else []

    video_times: list[float] = []
    audio_times: list[float] = []
    keyframe_times: list[float] = []
    video_regressions = 0
    audio_regressions = 0
    previous_video: float | None = None
    previous_audio: float | None = None

    for frame in frames:
        if not isinstance(frame, dict):
            continue
        media_type = str(frame.get("media_type", ""))
        timestamp = _frame_timestamp(frame)
        if media_type == "video":
            if timestamp is not None:
                if previous_video is not None and timestamp + 0.001 < previous_video:
                    video_regressions += 1
                previous_video = timestamp
                video_times.append(timestamp)
                if _safe_int(frame.get("key_frame")) == 1:
                    keyframe_times.append(timestamp)
        elif media_type == "audio" and timestamp is not None:
            if previous_audio is not None and timestamp + 0.001 < previous_audio:
                audio_regressions += 1
            previous_audio = timestamp
            audio_times.append(timestamp)

    video_span = _span(video_times)
    audio_span = _span(audio_times)
    video_fps = None
    if video_span is not None and video_span > 0 and len(video_times) >= 2:
        video_fps = (len(video_times) - 1) / video_span

    max_gop = _max_keyframe_gap(video_times, keyframe_times)
    reasons: list[str] = []
    warnings: list[str] = []

    if not video_times:
        reasons.append("VIDEO_TIMEOUT")
    if not audio_times:
        reasons.append("AUDIO_TIMEOUT")
    if video_regressions:
        reasons.append("VIDEO_TIMESTAMP_REGRESSION")
    if audio_regressions:
        reasons.append("AUDIO_TIMESTAMP_REGRESSION")

    required_progress = config.sample_seconds * config.min_progress_ratio
    if video_span is not None and video_span < required_progress:
        reasons.append("VIDEO_TIMESTAMP_STALLED")
    if audio_span is not None and audio_span < required_progress:
        reasons.append("AUDIO_TIMESTAMP_STALLED")

    if video_fps is None and video_times:
        warnings.append("FPS_UNKNOWN")
    elif video_fps is not None:
        if video_fps < config.min_video_fps or video_fps > config.max_video_fps:
            reasons.append("FPS_OUT_OF_RANGE")
        elif not (config.preferred_min_fps <= video_fps <= config.preferred_max_fps):
            warnings.append("FPS_NON_PREFERRED")

    if video_span is not None and video_times:
        if max_gop is not None and max_gop > config.max_gop_seconds:
            reasons.append("GOP_TOO_LONG")
        elif not keyframe_times:
            if video_span >= config.max_gop_seconds:
                reasons.append("KEYFRAME_TIMEOUT")
            else:
                warnings.append("GOP_UNOBSERVED")
        elif len(keyframe_times) == 1 and video_span < config.max_gop_seconds:
            warnings.append("GOP_UNOBSERVED")

    return {
        "sample_elapsed_seconds": round(
            sample_elapsed_seconds if sample_elapsed_seconds is not None else config.sample_seconds,
            3,
        ),
        "video_frames": len(video_times),
        "audio_frames": len(audio_times),
        "video_fps": round(video_fps, 2) if video_fps is not None else None,
        "video_timestamp_span_seconds": round(video_span, 3) if video_span is not None else None,
        "audio_timestamp_span_seconds": round(audio_span, 3) if audio_span is not None else None,
        "video_timestamp_regressions": video_regressions,
        "audio_timestamp_regressions": audio_regressions,
        "keyframes": len(keyframe_times),
        "max_gop_seconds": round(max_gop, 3) if max_gop is not None else None,
        "reasons": _dedupe(reasons),
        "warnings": _dedupe(warnings),
    }


def _parse_frame_output(output: str) -> dict[str, Any]:
    text = output.strip()
    if not text:
        return {"frames": []}

    # Keep JSON support for unit-test runners and compatibility with older
    # captured samples while runtime ffprobe uses compact line-oriented output.
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return {"frames": [], "invalid": True}
        return payload if isinstance(payload, dict) else {"frames": [], "invalid": True}

    frames: list[dict[str, Any]] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        frame: dict[str, Any] = {}
        for part in line.split("|"):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            if key:
                frame[key] = value
        if frame.get("media_type") in {"video", "audio"}:
            frames.append(frame)
    return {"frames": frames}


def _decode_subprocess_output(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    return ""


def _frame_timestamp(frame: dict[str, Any]) -> float | None:
    for key in ("best_effort_timestamp_time", "pkt_dts_time"):
        value = _safe_float(frame.get(key))
        if value is not None:
            return value
    return None


def _span(values: list[float]) -> float | None:
    if not values:
        return None
    return max(values) - min(values)


def _max_keyframe_gap(video_times: list[float], keyframe_times: list[float]) -> float | None:
    if not video_times:
        return None
    start = min(video_times)
    end = max(video_times)
    points = [start, *sorted(keyframe_times), end]
    if len(points) < 2:
        return None
    return max(max(0.0, right - left) for left, right in zip(points, points[1:]))


def _safe_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _safe_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))