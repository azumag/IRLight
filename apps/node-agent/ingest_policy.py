"""Inspect MediaMTX ingest media and enforce the Phase B input policy.

The inspector intentionally uses MediaMTX's internal Control API instead of
probing the public ingest endpoint. MediaMTX v1.20 exposes path ``tracks2``
(codec + codec properties), cumulative ``inboundBytes`` and source IDs. Those
are enough for the first policy slice:

- require H.264 video and MPEG-4 Audio (AAC)
- allow 1280x720 or 1920x1080 video
- warn when AAC is not 48 kHz stereo
- reject sustained aggregate ingest bitrate above the configured limit

FPS / GOP / timestamp progression are not exposed by this API and are left for
the next media-sampling slice.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


VIDEO_CODECS = {
    "AV1",
    "VP9",
    "VP8",
    "H265",
    "H264",
    "MPEG-4 Video",
    "MPEG-1/2 Video",
    "M-JPEG",
}
AUDIO_CODECS = {
    "Opus",
    "FLAC",
    "Vorbis",
    "MPEG-4 Audio",
    "MPEG-4 Audio LATM",
    "MPEG-1/2 Audio",
    "AC3",
    "Speex",
    "G726",
    "G722",
    "G711",
    "LPCM",
}
ALLOWED_RESOLUTIONS = {(1280, 720), (1920, 1080)}
SUPPORTED_SOURCE_TYPES = {"rtmpConn", "rtmpsConn", "srtConn"}


@dataclass(frozen=True)
class IngestPolicyConfig:
    api_url: str = "http://mediamtx:9997"
    path: str = "live/input"
    max_bitrate_bps: int = 6_000_000
    bitrate_violation_samples: int = 2
    timeout_seconds: float = 2.0

    @classmethod
    def from_env(cls) -> "IngestPolicyConfig":
        def env_int(name: str, default: int, minimum: int) -> int:
            try:
                return max(minimum, int(os.getenv(name, str(default))))
            except ValueError:
                return default

        try:
            timeout = float(os.getenv("NODE_MEDIAMTX_API_TIMEOUT_SECONDS", "2"))
        except ValueError:
            timeout = 2.0
        return cls(
            api_url=os.getenv("NODE_MEDIAMTX_API_URL", "http://mediamtx:9997").rstrip("/"),
            path=os.getenv("NODE_INGEST_PATH", "live/input"),
            max_bitrate_bps=env_int("NODE_INGEST_MAX_BITRATE_BPS", 6_000_000, 100_000),
            bitrate_violation_samples=env_int(
                "NODE_INGEST_BITRATE_VIOLATION_SAMPLES", 2, 1
            ),
            timeout_seconds=min(max(timeout, 0.2), 10.0),
        )


class IngestPolicyInspector:
    def __init__(self, config: IngestPolicyConfig | None = None) -> None:
        self.config = config or IngestPolicyConfig.from_env()
        self._last_source_id: str | None = None
        self._last_inbound_bytes: int | None = None
        self._last_sample_at: float | None = None
        self._over_bitrate_samples = 0
        self._last_enforced_source_id: str | None = None

    def _request_json(self, path: str, *, method: str = "GET") -> dict[str, Any]:
        request = urllib.request.Request(
            self.config.api_url + path,
            method=method,
            data=b"" if method != "GET" else None,
            headers={"Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"MediaMTX API HTTP {exc.code}: {body[:160]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"MediaMTX API unavailable: {exc.reason}") from exc
        if not raw:
            return {}
        try:
            value = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError("MediaMTX API returned invalid JSON") from exc
        return value if isinstance(value, dict) else {}

    def _reset_sampling(self) -> None:
        self._last_source_id = None
        self._last_inbound_bytes = None
        self._last_sample_at = None
        self._over_bitrate_samples = 0

    def _path_snapshot(self) -> dict[str, Any] | None:
        payload = self._request_json("/v3/paths/list?itemsPerPage=100")
        items = payload.get("items", [])
        if not isinstance(items, list):
            return None
        for item in items:
            if isinstance(item, dict) and item.get("name") == self.config.path:
                return item
        return None

    def observe(self, *, now: float | None = None) -> dict[str, Any]:
        observed_at = time.time() if now is None else now
        path = self._path_snapshot()
        if not path or not path.get("online") or not isinstance(path.get("source"), dict):
            self._reset_sampling()
            return {
                "status": "OFFLINE",
                "path": self.config.path,
                "online": False,
                "source_type": None,
                "source_id": None,
                "bitrate_bps": None,
                "tracks": [],
                "reasons": [],
                "warnings": [],
                "enforced": False,
                "observed_at": observed_at,
            }

        source = path["source"]
        source_id = str(source.get("id", "")) or None
        source_type = str(source.get("type", "")) or None
        tracks_raw = path.get("tracks2", [])
        tracks = _normalise_tracks(tracks_raw if isinstance(tracks_raw, list) else [])

        inbound_bytes = _safe_int(path.get("inboundBytes"))
        bitrate_bps: float | None = None
        if source_id != self._last_source_id:
            self._last_source_id = source_id
            self._last_inbound_bytes = inbound_bytes
            self._last_sample_at = observed_at
            self._over_bitrate_samples = 0
            self._last_enforced_source_id = None
        elif (
            inbound_bytes is not None
            and self._last_inbound_bytes is not None
            and self._last_sample_at is not None
            and observed_at > self._last_sample_at
            and inbound_bytes >= self._last_inbound_bytes
        ):
            elapsed = observed_at - self._last_sample_at
            bitrate_bps = (inbound_bytes - self._last_inbound_bytes) * 8.0 / elapsed
            self._last_inbound_bytes = inbound_bytes
            self._last_sample_at = observed_at

        reasons, warnings = evaluate_tracks(tracks)
        if bitrate_bps is not None:
            if bitrate_bps > self.config.max_bitrate_bps:
                self._over_bitrate_samples += 1
                if self._over_bitrate_samples >= self.config.bitrate_violation_samples:
                    reasons.append("BITRATE_TOO_HIGH")
                else:
                    warnings.append("BITRATE_ABOVE_LIMIT_PENDING")
            else:
                self._over_bitrate_samples = 0

        if reasons:
            status = "REJECTED"
        elif bitrate_bps is None:
            status = "PENDING"
        elif warnings:
            status = "WARNING"
        else:
            status = "ACCEPTED"

        return {
            "status": status,
            "path": self.config.path,
            "online": True,
            "source_type": source_type,
            "source_id": source_id,
            "bitrate_bps": round(bitrate_bps, 1) if bitrate_bps is not None else None,
            "max_bitrate_bps": self.config.max_bitrate_bps,
            "tracks": tracks,
            "reasons": reasons,
            "warnings": warnings,
            "enforced": False,
            "observed_at": observed_at,
        }

    def observe_and_enforce(self, *, now: float | None = None) -> dict[str, Any]:
        observation = self.observe(now=now)
        if observation["status"] != "REJECTED":
            return observation

        source_id = observation.get("source_id")
        source_type = observation.get("source_type")
        if (
            not isinstance(source_id, str)
            or not source_id
            or source_type not in SUPPORTED_SOURCE_TYPES
            or source_id == self._last_enforced_source_id
        ):
            return observation

        endpoint_prefix = {
            "rtmpConn": "rtmpconns",
            "rtmpsConn": "rtmpsconns",
            "srtConn": "srtconns",
        }[str(source_type)]
        encoded_id = urllib.parse.quote(source_id, safe="")
        try:
            self._request_json(f"/v3/{endpoint_prefix}/kick/{encoded_id}", method="POST")
            observation["enforced"] = True
            self._last_enforced_source_id = source_id
        except RuntimeError as exc:
            observation["enforcement_error"] = str(exc)[:160]
        return observation


def _safe_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _normalise_tracks(tracks: list[object]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in tracks[:8]:
        if not isinstance(item, dict):
            continue
        codec = str(item.get("codec", ""))
        props = item.get("codecProps")
        props = props if isinstance(props, dict) else {}
        track: dict[str, Any] = {"codec": codec}
        for key in ("width", "height", "profile", "level", "sampleRate", "channelCount"):
            if key in props:
                track[key] = props[key]
        result.append(track)
    return result


def evaluate_tracks(tracks: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    reasons: list[str] = []
    warnings: list[str] = []
    video = [track for track in tracks if track.get("codec") in VIDEO_CODECS]
    audio = [track for track in tracks if track.get("codec") in AUDIO_CODECS]

    if len(video) != 1 or video[0].get("codec") != "H264":
        reasons.append("VIDEO_CODEC_UNSUPPORTED")
    else:
        width = _safe_int(video[0].get("width"))
        height = _safe_int(video[0].get("height"))
        if width is None or height is None:
            warnings.append("VIDEO_DIMENSIONS_UNKNOWN")
        elif (width, height) not in ALLOWED_RESOLUTIONS:
            reasons.append("RESOLUTION_UNSUPPORTED")

    if len(audio) != 1 or audio[0].get("codec") != "MPEG-4 Audio":
        reasons.append("AUDIO_CODEC_UNSUPPORTED")
    else:
        sample_rate = _safe_int(audio[0].get("sampleRate"))
        channels = _safe_int(audio[0].get("channelCount"))
        if sample_rate is None:
            warnings.append("AUDIO_SAMPLE_RATE_UNKNOWN")
        elif sample_rate != 48_000:
            warnings.append("AUDIO_SAMPLE_RATE_NON_PREFERRED")
        if channels is None:
            warnings.append("AUDIO_CHANNELS_UNKNOWN")
        elif channels not in {1, 2}:
            reasons.append("AUDIO_CHANNELS_UNSUPPORTED")

    return reasons, warnings
