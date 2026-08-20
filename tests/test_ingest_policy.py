from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "node-agent"))

from ingest_policy import (  # noqa: E402
    IngestPolicyConfig,
    IngestPolicyInspector,
    evaluate_tracks,
)


def valid_tracks(*, width: int = 1280, height: int = 720, sample_rate: int = 48000):
    return [
        {
            "codec": "H264",
            "codecProps": {
                "width": width,
                "height": height,
                "profile": "High",
                "level": "4.1",
            },
        },
        {
            "codec": "MPEG-4 Audio",
            "codecProps": {"sampleRate": sample_rate, "channelCount": 2},
        },
    ]


def snapshot(
    *,
    inbound_bytes: int,
    tracks: list[dict[str, Any]] | None = None,
    source_type: str = "rtmpConn",
    source_id: str = "source-1",
) -> dict[str, Any]:
    return {
        "name": "live/input",
        "online": True,
        "inboundBytes": inbound_bytes,
        "source": {"type": source_type, "id": source_id},
        "tracks2": tracks if tracks is not None else valid_tracks(),
    }


class FakeInspector(IngestPolicyInspector):
    def __init__(self, snapshots: list[dict[str, Any] | None], **kwargs: Any) -> None:
        super().__init__(
            IngestPolicyConfig(
                api_url="http://mediamtx:9997",
                path="live/input",
                max_bitrate_bps=6_000_000,
                bitrate_violation_samples=2,
                timeout_seconds=1.0,
            )
        )
        self.snapshots = list(snapshots)
        self.kicks: list[str] = []

    def _path_snapshot(self) -> dict[str, Any] | None:
        if not self.snapshots:
            raise AssertionError("no fake snapshot left")
        return self.snapshots.pop(0)

    def _request_json(self, path: str, *, method: str = "GET") -> dict[str, Any]:
        if method == "POST":
            self.kicks.append(path)
            return {"status": "ok"}
        raise AssertionError(f"unexpected request {method} {path}")


class IngestPolicyTest(unittest.TestCase):
    def test_h264_aac_720p_is_accepted_after_bitrate_sample(self) -> None:
        inspector = FakeInspector(
            [snapshot(inbound_bytes=0), snapshot(inbound_bytes=1_500_000)]
        )
        first = inspector.observe(now=100.0)
        second = inspector.observe(now=110.0)
        self.assertEqual(first["status"], "PENDING")
        self.assertEqual(second["status"], "ACCEPTED")
        self.assertAlmostEqual(second["bitrate_bps"], 1_200_000.0)
        self.assertEqual(second["reasons"], [])

    def test_h265_is_rejected_and_rtmp_source_is_kicked(self) -> None:
        tracks = valid_tracks()
        tracks[0]["codec"] = "H265"
        inspector = FakeInspector([snapshot(inbound_bytes=0, tracks=tracks)])
        result = inspector.observe_and_enforce(now=100.0)
        self.assertEqual(result["status"], "REJECTED")
        self.assertIn("VIDEO_CODEC_UNSUPPORTED", result["reasons"])
        self.assertTrue(result["enforced"])
        self.assertEqual(inspector.kicks, ["/v3/rtmpconns/kick/source-1"])

    def test_unsupported_resolution_is_rejected(self) -> None:
        inspector = FakeInspector(
            [snapshot(inbound_bytes=0, tracks=valid_tracks(width=640, height=360))]
        )
        result = inspector.observe_and_enforce(now=100.0)
        self.assertEqual(result["status"], "REJECTED")
        self.assertIn("RESOLUTION_UNSUPPORTED", result["reasons"])

    def test_44100_aac_is_warning_not_rejection(self) -> None:
        inspector = FakeInspector(
            [
                snapshot(inbound_bytes=0, tracks=valid_tracks(sample_rate=44100)),
                snapshot(inbound_bytes=1_000_000, tracks=valid_tracks(sample_rate=44100)),
            ]
        )
        inspector.observe(now=100.0)
        result = inspector.observe(now=110.0)
        self.assertEqual(result["status"], "WARNING")
        self.assertIn("AUDIO_SAMPLE_RATE_NON_PREFERRED", result["warnings"])
        self.assertEqual(result["reasons"], [])

    def test_sustained_bitrate_above_limit_is_rejected_on_second_sample(self) -> None:
        inspector = FakeInspector(
            [
                snapshot(inbound_bytes=0),
                snapshot(inbound_bytes=10_000_000),
                snapshot(inbound_bytes=20_000_000),
            ]
        )
        inspector.observe(now=100.0)
        first_high = inspector.observe(now=110.0)
        second_high = inspector.observe_and_enforce(now=120.0)
        self.assertEqual(first_high["status"], "WARNING")
        self.assertIn("BITRATE_ABOVE_LIMIT_PENDING", first_high["warnings"])
        self.assertEqual(second_high["status"], "REJECTED")
        self.assertIn("BITRATE_TOO_HIGH", second_high["reasons"])
        self.assertTrue(second_high["enforced"])

    def test_srt_rejection_uses_srt_kick_endpoint(self) -> None:
        tracks = valid_tracks()
        tracks[1]["codec"] = "Opus"
        inspector = FakeInspector(
            [snapshot(inbound_bytes=0, tracks=tracks, source_type="srtConn")]
        )
        result = inspector.observe_and_enforce(now=100.0)
        self.assertEqual(result["status"], "REJECTED")
        self.assertEqual(inspector.kicks, ["/v3/srtconns/kick/source-1"])

    def test_new_srt_source_graces_first_partial_track_snapshot(self) -> None:
        partial = [valid_tracks()[0]]
        inspector = FakeInspector(
            [
                snapshot(
                    inbound_bytes=0,
                    tracks=partial,
                    source_type="srtConn",
                    source_id="srt-reconnect-1",
                ),
                snapshot(
                    inbound_bytes=1_500_000,
                    tracks=valid_tracks(),
                    source_type="srtConn",
                    source_id="srt-reconnect-1",
                ),
            ]
        )
        first = inspector.observe_and_enforce(now=100.0)
        second = inspector.observe_and_enforce(now=110.0)
        self.assertEqual(first["status"], "PENDING")
        self.assertIn("TRACK_METADATA_PENDING", first["warnings"])
        self.assertEqual(first["reasons"], [])
        self.assertFalse(first["enforced"])
        self.assertEqual(second["status"], "ACCEPTED")
        self.assertEqual(inspector.kicks, [])

    def test_partial_track_snapshot_is_rejected_if_it_persists(self) -> None:
        partial = [valid_tracks()[0]]
        inspector = FakeInspector(
            [
                snapshot(inbound_bytes=0, tracks=partial, source_type="srtConn"),
                snapshot(inbound_bytes=100_000, tracks=partial, source_type="srtConn"),
            ]
        )
        first = inspector.observe_and_enforce(now=100.0)
        second = inspector.observe_and_enforce(now=110.0)
        self.assertEqual(first["status"], "PENDING")
        self.assertEqual(second["status"], "REJECTED")
        self.assertIn("AUDIO_CODEC_UNSUPPORTED", second["reasons"])
        self.assertTrue(second["enforced"])
        self.assertEqual(inspector.kicks, ["/v3/srtconns/kick/source-1"])

    def test_new_source_does_not_grace_known_bad_codec(self) -> None:
        tracks = valid_tracks()
        tracks[1]["codec"] = "Opus"
        inspector = FakeInspector(
            [snapshot(inbound_bytes=0, tracks=tracks, source_type="srtConn")]
        )
        result = inspector.observe_and_enforce(now=100.0)
        self.assertEqual(result["status"], "REJECTED")
        self.assertTrue(result["enforced"])

    def test_offline_resets_bitrate_history(self) -> None:
        inspector = FakeInspector(
            [snapshot(inbound_bytes=0), None, snapshot(inbound_bytes=99_000_000)]
        )
        inspector.observe(now=100.0)
        offline = inspector.observe(now=110.0)
        after_reconnect = inspector.observe(now=120.0)
        self.assertEqual(offline["status"], "OFFLINE")
        self.assertEqual(after_reconnect["status"], "PENDING")
        self.assertIsNone(after_reconnect["bitrate_bps"])

    def test_track_policy_requires_single_h264_and_aac(self) -> None:
        reasons, warnings = evaluate_tracks(
            [{"codec": "H264", "width": 1920, "height": 1080}]
        )
        self.assertIn("AUDIO_CODEC_UNSUPPORTED", reasons)
        self.assertEqual(warnings, [])


if __name__ == "__main__":
    unittest.main()
