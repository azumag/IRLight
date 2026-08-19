from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "node-agent"))

from ingest_quality import IngestQualityConfig, evaluate_frame_sample  # noqa: E402


def frame(media_type: str, timestamp: float, *, key_frame: int = 0) -> dict[str, object]:
    return {
        "media_type": media_type,
        "key_frame": key_frame,
        "best_effort_timestamp_time": timestamp,
        "pkt_dts_time": timestamp,
    }


def healthy_video() -> list[dict[str, object]]:
    return [
        frame("video", index / 30.0, key_frame=1 if index % 30 == 0 else 0)
        for index in range(61)
    ]


def healthy_audio() -> list[dict[str, object]]:
    return [frame("audio", index / 50.0) for index in range(101)]


class EffectiveTimeoutClassificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = IngestQualityConfig(sample_seconds=2.0, min_bitrate_bps=0)

    def test_negligible_audio_residual_progress_is_timeout(self) -> None:
        payload = {
            "frames": [
                *healthy_video(),
                frame("audio", 10.000),
                frame("audio", 10.021),
            ]
        }
        result = evaluate_frame_sample(
            payload,
            self.config,
            sample_elapsed_seconds=2.15,
        )

        self.assertIn("AUDIO_TIMEOUT", result["reasons"])
        self.assertNotIn("AUDIO_TIMESTAMP_STALLED", result["reasons"])

    def test_negligible_video_residual_progress_is_timeout(self) -> None:
        payload = {
            "frames": [
                frame("video", 20.000, key_frame=1),
                frame("video", 20.033),
                *healthy_audio(),
            ]
        }
        result = evaluate_frame_sample(
            payload,
            self.config,
            sample_elapsed_seconds=2.15,
        )

        self.assertIn("VIDEO_TIMEOUT", result["reasons"])
        self.assertNotIn("VIDEO_TIMESTAMP_STALLED", result["reasons"])

    def test_meaningful_but_insufficient_progress_remains_stalled(self) -> None:
        payload = {
            "frames": [
                *healthy_video(),
                frame("audio", 30.0),
                frame("audio", 30.4),
                frame("audio", 30.8),
            ]
        }
        result = evaluate_frame_sample(
            payload,
            self.config,
            sample_elapsed_seconds=2.15,
        )

        self.assertIn("AUDIO_TIMESTAMP_STALLED", result["reasons"])
        self.assertNotIn("AUDIO_TIMEOUT", result["reasons"])

    def test_short_partial_sample_does_not_promote_stall_to_timeout(self) -> None:
        payload = {
            "frames": [
                *healthy_video(),
                frame("audio", 40.000),
                frame("audio", 40.021),
            ]
        }
        result = evaluate_frame_sample(
            payload,
            self.config,
            sample_elapsed_seconds=1.0,
        )

        self.assertIn("AUDIO_TIMESTAMP_STALLED", result["reasons"])
        self.assertNotIn("AUDIO_TIMEOUT", result["reasons"])


if __name__ == "__main__":
    unittest.main()
