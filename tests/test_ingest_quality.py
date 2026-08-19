from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "node-agent"))

from ingest_quality import (  # noqa: E402
    IngestQualityConfig,
    IngestQualitySampler,
    evaluate_frame_sample,
)


def frame_payload(
    *,
    fps: int = 30,
    duration: float = 4.0,
    key_interval_seconds: float = 2.0,
    include_video: bool = True,
    include_audio: bool = True,
) -> dict[str, object]:
    frames: list[dict[str, object]] = []
    if include_video:
        count = int(duration * fps) + 1
        key_every = max(1, int(fps * key_interval_seconds))
        for index in range(count):
            frames.append(
                {
                    "media_type": "video",
                    "key_frame": 1 if index % key_every == 0 else 0,
                    "best_effort_timestamp_time": f"{index / fps:.6f}",
                }
            )
    if include_audio:
        audio_fps = 50
        for index in range(int(duration * audio_fps) + 1):
            frames.append(
                {
                    "media_type": "audio",
                    "key_frame": 1,
                    "best_effort_timestamp_time": f"{index / audio_fps:.6f}",
                }
            )
    # ffprobe interleaves frames by time rather than media type.
    frames.sort(key=lambda item: float(str(item["best_effort_timestamp_time"])))
    return {"frames": frames}


class IngestQualityEvaluationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = IngestQualityConfig(sample_seconds=4.0)

    def test_healthy_30fps_stream_is_accepted_by_quality_rules(self) -> None:
        result = evaluate_frame_sample(frame_payload(), self.config)
        self.assertEqual(result["reasons"], [])
        self.assertAlmostEqual(result["video_fps"], 30.0, places=1)
        self.assertGreaterEqual(result["keyframes"], 2)
        self.assertLessEqual(result["max_gop_seconds"], 2.1)

    def test_low_fps_is_degraded(self) -> None:
        result = evaluate_frame_sample(frame_payload(fps=10), self.config)
        self.assertIn("FPS_OUT_OF_RANGE", result["reasons"])

    def test_audio_only_gap_is_detected(self) -> None:
        result = evaluate_frame_sample(
            frame_payload(include_audio=False), self.config
        )
        self.assertIn("AUDIO_TIMEOUT", result["reasons"])

    def test_video_timestamp_regression_is_detected(self) -> None:
        payload = frame_payload(duration=2.0)
        frames = payload["frames"]
        assert isinstance(frames, list)
        video = [item for item in frames if item.get("media_type") == "video"]
        video[5]["best_effort_timestamp_time"] = "0.010000"
        result = evaluate_frame_sample(payload, self.config)
        self.assertIn("VIDEO_TIMESTAMP_REGRESSION", result["reasons"])

    def test_long_gop_is_degraded(self) -> None:
        result = evaluate_frame_sample(
            frame_payload(duration=5.0, key_interval_seconds=5.0),
            self.config,
        )
        self.assertIn("GOP_TOO_LONG", result["reasons"])


class IngestQualitySamplerTest(unittest.TestCase):
    def test_low_bitrate_requires_consecutive_samples(self) -> None:
        payload = json.dumps(frame_payload())

        def runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess([], 0, stdout=payload, stderr="")

        sampler = IngestQualitySampler(
            IngestQualityConfig(
                sample_seconds=4.0,
                min_bitrate_bps=500_000,
                low_bitrate_samples=2,
            ),
            runner=runner,
        )
        observation = {
            "status": "ACCEPTED",
            "online": True,
            "source_id": "source-1",
            "source_type": "rtmpConn",
            "bitrate_bps": 400_000,
            "tracks": [],
            "reasons": [],
            "warnings": [],
        }

        first = sampler.augment(observation)
        self.assertEqual(first["status"], "WARNING")
        self.assertIn("BITRATE_BELOW_MIN_PENDING", first["warnings"])

        second = sampler.augment(observation)
        self.assertEqual(second["status"], "DEGRADED")
        self.assertIn("BITRATE_TOO_LOW", second["reasons"])

    def test_new_source_resets_low_bitrate_streak(self) -> None:
        payload = json.dumps(frame_payload())

        def runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess([], 0, stdout=payload, stderr="")

        sampler = IngestQualitySampler(
            IngestQualityConfig(low_bitrate_samples=2), runner=runner
        )
        base = {
            "status": "ACCEPTED",
            "online": True,
            "source_type": "rtmpConn",
            "bitrate_bps": 100_000,
            "tracks": [],
            "reasons": [],
            "warnings": [],
        }
        sampler.augment({**base, "source_id": "one"})
        changed = sampler.augment({**base, "source_id": "two"})
        self.assertNotIn("BITRATE_TOO_LOW", changed["reasons"])


if __name__ == "__main__":
    unittest.main()
