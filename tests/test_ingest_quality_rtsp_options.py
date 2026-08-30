from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


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

        def runner(command: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            assert isinstance(command, list)
            selector = str(command[command.index("-select_streams") + 1])
            manifest = str(kwargs.get("input", ""))
            allowed = "video" if "allowed_media_types video" in manifest else "audio"
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

    def test_authenticated_url_is_sent_over_stdin_not_process_argv(self) -> None:
        protected_url = "rtsp://irlight-internal:super-secret@mediamtx:8554/live/input"
        observed: dict[str, object] = {}

        def runner(command: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            observed["command"] = command
            observed["input"] = kwargs.get("input")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        IngestQualitySampler(
            IngestQualityConfig(input_url=protected_url, sample_seconds=1.0),
            runner=runner,
        ).sample()

        self.assertNotIn("super-secret", " ".join(observed["command"]))
        self.assertIn("super-secret", str(observed["input"]))

    def test_authenticated_url_file_is_resolved_only_when_sampling(self) -> None:
        protected_url = "rtsp://irlight-internal:late-secret@mediamtx:8554/live/input"
        observed: dict[str, object] = {}

        def runner(command: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            observed["command"] = command
            observed["input"] = kwargs.get("input")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as directory:
            secret_path = Path(directory) / "media_input_uri"
            with patch.dict(
                "os.environ",
                {"NODE_INGEST_SAMPLE_URL_FILE": str(secret_path)},
                clear=False,
            ):
                config = IngestQualityConfig.from_env()
            sampler = IngestQualitySampler(config, runner=runner)

            # Bootstrap has not materialized the file yet, but agent construction
            # remains safe and a sample fails closed without invoking ffprobe.
            unavailable = sampler.sample()
            self.assertEqual(unavailable["reasons"], ["MEDIA_SAMPLE_FAILED"])
            self.assertEqual(observed, {})

            secret_path.write_text(protected_url + "\n", encoding="utf-8")
            sampler.sample()

        self.assertNotIn("late-secret", " ".join(observed["command"]))
        self.assertIn("late-secret", str(observed["input"]))


if __name__ == "__main__":
    unittest.main()
