from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from node_internal import IngestObservationRequest, _append_ingest_events  # noqa: E402


class NodeIngestEventTest(unittest.TestCase):
    def test_rejection_becomes_control_plane_node_event(self) -> None:
        node = {"events": []}
        previous = {
            "status": "PENDING",
            "online": True,
            "source_id": "source-1",
        }
        current = IngestObservationRequest(
            status="REJECTED",
            path="live/input",
            online=True,
            source_type="rtmpConn",
            source_id="source-1",
            bitrate_bps=8_000_000,
            max_bitrate_bps=6_000_000,
            tracks=[
                {"codec": "H264", "width": 1280, "height": 720},
                {"codec": "MPEG-4 Audio", "sampleRate": 48000, "channelCount": 2},
            ],
            reasons=["BITRATE_TOO_HIGH"],
            warnings=[],
            enforced=True,
            observed_at=1.0,
        ).model_dump()
        _append_ingest_events(node, previous, current)
        self.assertEqual(node["events"][0]["type"], "ingest.rejected")
        self.assertTrue(node["events"][0]["payload"]["enforced"])

    def test_new_source_emits_format_detected_once(self) -> None:
        node = {"events": []}
        current = {
            "status": "PENDING",
            "online": True,
            "source_id": "source-1",
            "source_type": "rtmpConn",
            "bitrate_bps": None,
            "tracks": [],
            "reasons": [],
            "warnings": [],
            "enforced": False,
        }
        _append_ingest_events(node, {}, current)
        _append_ingest_events(node, current, current)
        self.assertEqual(
            [event["type"] for event in node["events"]],
            ["ingest.format_detected"],
        )

    def test_disconnect_emits_event(self) -> None:
        node = {"events": []}
        previous = {"status": "ACCEPTED", "online": True, "source_id": "source-1"}
        current = {
            "status": "OFFLINE",
            "online": False,
            "source_id": None,
            "source_type": None,
            "bitrate_bps": None,
            "tracks": [],
            "reasons": [],
            "warnings": [],
            "enforced": False,
        }
        _append_ingest_events(node, previous, current)
        self.assertEqual(node["events"][0]["type"], "ingest.disconnected")

    def test_degraded_and_recovered_are_auditable(self) -> None:
        node = {"events": []}
        accepted = {
            "status": "ACCEPTED",
            "online": True,
            "source_id": "source-1",
            "source_type": "rtmpConn",
            "bitrate_bps": 2_000_000,
            "tracks": [],
            "quality": {"video_fps": 30.0},
            "reasons": [],
            "warnings": [],
            "enforced": False,
        }
        degraded = {
            **accepted,
            "status": "DEGRADED",
            "quality": {"video_fps": 10.0},
            "reasons": ["FPS_OUT_OF_RANGE"],
        }
        recovered = {**accepted, "quality": {"video_fps": 30.0}}
        _append_ingest_events(node, accepted, degraded)
        _append_ingest_events(node, degraded, recovered)
        self.assertEqual(
            [event["type"] for event in node["events"]],
            ["ingest.degraded", "ingest.recovered"],
        )
        self.assertEqual(
            node["events"][0]["payload"]["quality"]["video_fps"], 10.0
        )

    def test_new_degraded_source_emits_format_and_degraded(self) -> None:
        node = {"events": []}
        degraded = {
            "status": "DEGRADED",
            "online": True,
            "source_id": "source-2",
            "source_type": "rtmpConn",
            "bitrate_bps": 1_000_000,
            "tracks": [],
            "quality": {"video_fps": 10.0},
            "reasons": ["FPS_OUT_OF_RANGE"],
            "warnings": [],
            "enforced": False,
        }
        _append_ingest_events(node, {}, degraded)
        self.assertEqual(
            [event["type"] for event in node["events"]],
            ["ingest.format_detected", "ingest.degraded"],
        )


if __name__ == "__main__":
    unittest.main()
