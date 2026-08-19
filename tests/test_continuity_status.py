from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "node-agent"))

from continuity_status import read_continuity_status  # noqa: E402


class ContinuityStatusReaderTest(unittest.TestCase):
    def test_reader_returns_only_safe_actual_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "status.json")
            path.write_text(
                json.dumps(
                    {
                        "session_status": "LIVE",
                        "video_source": "LIVE",
                        "desired_audio_mode": "MUTED",
                        "actual_audio_mode": "MUTED",
                        "input_video_recent": True,
                        "input_audio_recent": True,
                        "started_at": 100.0,
                        "updated_at": 120.0,
                        "egress": "rtmp://example.invalid/secret",
                        "command_id": "private-command",
                    }
                ),
                encoding="utf-8",
            )
            result = read_continuity_status(path, now=125.0, max_age_seconds=30.0)
        self.assertEqual(result["session_status"], "LIVE")
        self.assertEqual(result["video_source"], "LIVE")
        self.assertEqual(result["desired_audio_mode"], "MUTED")
        self.assertNotIn("egress", result)
        self.assertNotIn("command_id", result)
        self.assertNotIn("secret", str(result))

    def test_stale_status_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "status.json")
            path.write_text(
                json.dumps(
                    {
                        "session_status": "LIVE",
                        "video_source": "LIVE",
                        "desired_audio_mode": "LIVE",
                        "actual_audio_mode": "LIVE",
                        "updated_at": 100.0,
                    }
                ),
                encoding="utf-8",
            )
            result = read_continuity_status(path, now=131.0, max_age_seconds=30.0)
        self.assertEqual(result["session_status"], "UNKNOWN")
        self.assertEqual(result["reason_code"], "STATUS_STALE")

    def test_missing_corrupt_or_invalid_status_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = read_continuity_status(Path(tmp, "missing.json"))
            self.assertEqual(missing["reason_code"], "STATUS_UNAVAILABLE")

            path = Path(tmp, "status.json")
            path.write_text("{broken", encoding="utf-8")
            corrupt = read_continuity_status(path)
            self.assertEqual(corrupt["session_status"], "UNKNOWN")

            path.write_text(
                json.dumps(
                    {
                        "session_status": "BROKEN",
                        "video_source": "LIVE",
                        "desired_audio_mode": "LIVE",
                        "actual_audio_mode": "LIVE",
                        "updated_at": 100.0,
                    }
                ),
                encoding="utf-8",
            )
            invalid = read_continuity_status(path, now=100.0)
            self.assertEqual(invalid["reason_code"], "STATUS_INVALID")


if __name__ == "__main__":
    unittest.main()
