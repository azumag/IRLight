from __future__ import annotations

import sys
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

import node_internal  # noqa: E402
from node_internal import HeartbeatRequest, IngestObservationRequest  # noqa: E402


class NodeSessionRecoveryTickTest(unittest.TestCase):
    def test_unchanged_ingest_heartbeat_still_reaches_session_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nodes_path = root / "nodes.json"
            tokens_path = root / "bootstrap_tokens.json"
            current = {
                "status": "ACCEPTED",
                "path": "live/input",
                "online": True,
                "source_type": "rtmpConn",
                "source_id": "source-2",
                "bitrate_bps": 1_500_000,
                "max_bitrate_bps": 6_000_000,
                "tracks": [],
                "reasons": [],
                "warnings": [],
                "quality": {"video_fps": 30.0},
                "enforced": False,
                "enforcement_error": None,
                "observed_at": 120.0,
            }
            node_internal.atomic_write_json(
                nodes_path,
                {
                    "nodes": {
                        "node-0001": {
                            "node_id": "node-0001",
                            "session_id": "session-1",
                            "session_assigned": True,
                            "status": "READY",
                            "desired_state": "RUNNING",
                            "ingest": dict(current),
                            "ingest_ever_online": True,
                            "events": [],
                            "next_event_seq": 1,
                            "access_token_sha256": hashlib.sha256(
                                b"node-access-token"
                            ).hexdigest(),
                        }
                    },
                    "next_node_seq": 2,
                    "tokens": {},
                },
            )
            store = MagicMock()
            store.apply_ingest_observation.return_value = {"status": "HOLDING"}

            with (
                patch.object(node_internal, "STATE_DIR", root),
                patch.object(node_internal, "NODES_PATH", nodes_path),
                patch.object(node_internal, "TOKENS_PATH", tokens_path),
                patch.object(node_internal, "default_store", return_value=store),
            ):
                node_internal.heartbeat(
                    "node-0001",
                    HeartbeatRequest(
                        status="READY",
                        media_health="running",
                        active_publisher=True,
                        egress_connected=True,
                        ingest=IngestObservationRequest(**current),
                    ),
                    authorization="Bearer node-access-token",
                )

            store.apply_ingest_observation.assert_called_once()
            kwargs = store.apply_ingest_observation.call_args.kwargs
            self.assertEqual(kwargs["event_types"], [])
            self.assertEqual(kwargs["observation"]["status"], "ACCEPTED")
            self.assertEqual(kwargs["observation"]["source_id"], "source-2")


if __name__ == "__main__":
    unittest.main()
