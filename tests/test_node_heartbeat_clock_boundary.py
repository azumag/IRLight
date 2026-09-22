from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

import node_internal  # noqa: E402
from node_internal import (  # noqa: E402
    EgressObservationRequest,
    HeartbeatRequest,
    IngestObservationRequest,
    RelayClientObservationRequest,
)


class _RecordingStore:
    def __init__(self) -> None:
        self.heartbeat_at: float | None = None
        self.ingest_at: float | None = None
        self.egress_event_times: list[float] = []
        self.relay_observations: list[dict[str, object]] = []

    def record_node_heartbeat(self, _session_id: str, **kwargs: object) -> bool:
        self.heartbeat_at = float(kwargs["observed_at"])
        return True

    def apply_ingest_observation(self, _session_id: str, **kwargs: object) -> dict[str, object]:
        self.ingest_at = float(kwargs["occurred_at"])
        return {}

    def get(self, _session_id: str) -> dict[str, object]:
        return {"node_id": "node-0001", "status": "LIVE"}

    def append_event(self, _session_id: str, **kwargs: object) -> dict[str, object]:
        self.egress_event_times.append(float(kwargs["occurred_at"]))
        return {}

    def update(self, _session_id: str, **_changes: object) -> dict[str, object]:
        return {}

    def apply_relay_client_observation(
        self, _session_id: str, **kwargs: object
    ) -> dict[str, object]:
        self.relay_observations.append(dict(kwargs))
        return {}


class NodeHeartbeatClockBoundaryTest(unittest.TestCase):
    @staticmethod
    def _request(*, agent_observed_at: float = 10.0) -> HeartbeatRequest:
        return HeartbeatRequest(
            status="READY",
            media_health="running",
            active_publisher=True,
            egress_connected=True,
            ingest=IngestObservationRequest(
                status="ACCEPTED",
                path="live/input",
                online=True,
                source_type="rtmpConn",
                source_id="source-1",
                observed_at=agent_observed_at,
            ),
            egress=EgressObservationRequest(
                status="CONNECTED",
                connected=True,
                attempt=1,
                rendered_buffers=10,
                observed_at=agent_observed_at,
            ),
            relay_client=RelayClientObservationRequest(
                status="CONNECTED",
                connected=True,
                reader_count=1,
                observed_at=agent_observed_at,
            ),
        )

    @staticmethod
    def _authority() -> dict[str, object]:
        return {
            "nodes": {
                "node-0001": {
                    "node_id": "node-0001",
                    "session_id": "session-1",
                    "session_assigned": True,
                    "boot_id": "boot-1",
                    "status": "READY",
                    "desired_state": "RUNNING",
                    "last_heartbeat_at": None,
                    "ingest": None,
                    "ingest_ever_online": False,
                    "egress": None,
                    "egress_ever_connected": False,
                    "relay_client": None,
                    "relay_client_ever_connected": False,
                    "events": [],
                    "next_event_seq": 1,
                }
            },
            "tokens": {},
            "next_node_seq": 2,
        }

    def test_agent_observation_timestamps_reject_pre_epoch_values(self) -> None:
        constructors = (
            lambda value: IngestObservationRequest(
                status="OFFLINE", path="live/input", observed_at=value
            ),
            lambda value: EgressObservationRequest(status="UNKNOWN", observed_at=value),
            lambda value: RelayClientObservationRequest(status="UNKNOWN", observed_at=value),
        )
        for constructor in constructors:
            with self.subTest(constructor=constructor):
                with self.assertRaises(ValidationError):
                    constructor(-0.001)
                self.assertEqual(constructor(0.0).observed_at, 0.0)

    def test_heartbeat_samples_control_plane_clock_once_for_node_events(self) -> None:
        authority = self._authority()
        store = _RecordingStore()
        with (
            patch.object(node_internal, "_read_authority", return_value=authority),
            patch.object(node_internal, "_write_authority"),
            patch.object(node_internal, "_require_node_access"),
            patch.object(node_internal, "default_store", return_value=store),
            patch.object(node_internal, "apply_pipeline_health"),
            patch.object(
                node_internal.time,
                "time",
                side_effect=[123.0, AssertionError("heartbeat clock sampled more than once")],
            ),
        ):
            node_internal._heartbeat_locked(
                "node-0001",
                self._request(agent_observed_at=10.0),
                "Bearer ignored-by-test",
            )

        node = authority["nodes"]["node-0001"]
        self.assertEqual(node["last_heartbeat_at"], 123.0)
        self.assertEqual(
            [event["type"] for event in node["events"]],
            [
                "ingest.connected",
                "ingest.format_detected",
                "egress.connected",
                "relay.client.connected",
            ],
        )
        self.assertTrue(all(event["occurred_at"] == 123.0 for event in node["events"]))
        self.assertEqual(node["ingest"]["observed_at"], 10.0)
        self.assertEqual(node["egress"]["observed_at"], 10.0)
        self.assertEqual(node["relay_client"]["observed_at"], 10.0)
        self.assertEqual(store.heartbeat_at, 123.0)
        self.assertEqual(store.ingest_at, 123.0)
        self.assertEqual(store.egress_event_times, [123.0])

    def test_epoch_zero_is_valid_for_control_plane_event_clock(self) -> None:
        authority = self._authority()
        store = _RecordingStore()
        with (
            patch.object(node_internal, "_read_authority", return_value=authority),
            patch.object(node_internal, "_write_authority"),
            patch.object(node_internal, "_require_node_access"),
            patch.object(node_internal, "default_store", return_value=store),
            patch.object(node_internal, "apply_pipeline_health"),
            patch.object(node_internal.time, "time", return_value=0.0),
        ):
            node_internal._heartbeat_locked(
                "node-0001",
                self._request(agent_observed_at=0.0),
                "Bearer ignored-by-test",
            )

        node = authority["nodes"]["node-0001"]
        self.assertEqual(node["last_heartbeat_at"], 0.0)
        self.assertTrue(all(event["occurred_at"] == 0.0 for event in node["events"]))
        self.assertEqual(store.ingest_at, 0.0)
        self.assertEqual(store.egress_event_times, [0.0])


if __name__ == "__main__":
    unittest.main()
