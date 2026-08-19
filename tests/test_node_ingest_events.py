from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from node_internal import IngestObservationRequest, _append_ingest_event  # noqa: E402


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
        _append_ingest_event(node, previous, current)
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
        _append_ingest_event(node, {}, current)
        _append_ingest_event(node, current, current)
        self.assertEqual([event["type"] for event in node["events"]], ["ingest.format_detected"])

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
        _append_ingest_event(node, previous, current)
        self.assertEqual(node["events"][0]["type"], "ingest.disconnected")


if __name__ == "__main__":
    unittest.main()
