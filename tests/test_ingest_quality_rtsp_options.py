from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "node-agent"))

from ingest_quality import IngestQualityConfig, IngestQualitySampler  # noqa: E402


def compact_frames(media_type: str, *, fps: int, duration: float) -> str:
    lines = []
    for index in range(int(duration * fps) + 1):
        lines.append(
            "|".join(
                [
                    f"media_type={media_type}",
                    f"key_frame={1 if index == 0 else 0}",
                    f"best_effort_timestamp_time={index / fps:.6f}",
                    f"pkt_dts_time={index / fps:.6f}",
                ]
            )
        )
    return "\n".join(lines) + "\n"


class IngestQualityRtspOptionsTest(unittest.TestCase):
    def test_each_probe_restricts_rtsp_input_to_its_media_type(self) -> None:
        observed: dict[str, str] = {}

        def runner(command: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            assert isinstance(command, list)
            selector = str(command[command.index("-select_streams") + 1])
            allowed = str(command[command.index("-allowed_media_types") + 1])
            observed[selector] = allowed
            media_type = "video" if selector == "v:0" else "audio"
            fps = 30 if media_type == "video" else 50
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=compact_frames(media_type, fps=fps, duration=2.0),
                stderr="",
            )

        sampler = IngestQualitySampler(
            IngestQualityConfig(
                sample_seconds=2.0,
                min_bitrate_bps=0,
            ),
            runner=runner,
        )
        result = sampler.sample()

        self.assertEqual(observed, {"v:0": "video", "a:0": "audio"})
        self.assertNotIn("VIDEO_TIMEOUT", result["reasons"])
        self.assertNotIn("AUDIO_TIMEOUT", result["reasons"])


if __name__ == "__main__":
    unittest.main()
