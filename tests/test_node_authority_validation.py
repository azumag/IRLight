from __future__ import annotations

import copy
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

import node_internal  # noqa: E402


def valid_authority() -> dict[str, object]:
    return {
        "nodes": {
            "node-0001": {
                "node_id": "node-0001",
                "session_id": "session-1",
                "session_assigned": True,
                "destination_id": "destination-1",
                "egress_mode": "DIRECT_PUSH",
                "provider_server_id": "provider-1",
                "boot_id": "boot-1",
                "agent_version": "1.0",
                "status": "READY",
                "desired_state": "RUNNING",
                "absolute_deadline": 2_000.0,
                "last_heartbeat_at": 1_000.0,
                "ingest": None,
                "egress": None,
                "relay_client": None,
                "events": [],
                "next_event_seq": 1,
                "created_at": 900.0,
                "access_token_sha256": "0" * 64,
            }
        },
        "next_node_seq": 2,
        "tokens": {},
    }


class NodeAuthorityValidationTest(unittest.TestCase):
    def test_valid_authority_is_accepted_without_mutation(self) -> None:
        state = valid_authority()
        before = copy.deepcopy(state)
        self.assertIs(node_internal.validate_node_authority(state), state)
        self.assertEqual(state, before)

    def test_legacy_optional_node_fields_may_be_absent(self) -> None:
        state = valid_authority()
        node = state["nodes"]["node-0001"]
        for field in (
            "session_assigned",
            "destination_id",
            "egress_mode",
            "ingest",
            "egress",
            "relay_client",
            "events",
            "next_event_seq",
        ):
            node.pop(field, None)
        node_internal.validate_node_authority(state)

    def test_identity_enum_counter_and_timestamp_corruption_fail_closed(self) -> None:
        mutations = {
            "sequence bool": lambda state: state.__setitem__("next_node_seq", True),
            "sequence string": lambda state: state.__setitem__("next_node_seq", "2"),
            "stale sequence": lambda state: state.__setitem__("next_node_seq", 1),
            "unknown status": lambda state: state["nodes"]["node-0001"].__setitem__(
                "status", "MAYBE"
            ),
            "unknown desired state": lambda state: state["nodes"]["node-0001"].__setitem__(
                "desired_state", "DELETE"
            ),
            "unknown egress mode": lambda state: state["nodes"]["node-0001"].__setitem__(
                "egress_mode", "BOTH"
            ),
            "bool deadline": lambda state: state["nodes"]["node-0001"].__setitem__(
                "absolute_deadline", True
            ),
            "string heartbeat": lambda state: state["nodes"]["node-0001"].__setitem__(
                "last_heartbeat_at", "1000"
            ),
            "non-finite metric": lambda state: state["nodes"]["node-0001"].__setitem__(
                "memory_mb", math.inf
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                state = valid_authority()
                mutate(state)
                with self.assertRaises(node_internal.NodeStateError):
                    node_internal.validate_node_authority(state)

    def test_nested_observation_and_event_corruption_fail_closed(self) -> None:
        state = valid_authority()
        node = state["nodes"]["node-0001"]
        node["ingest"] = {
            "status": "ACCEPTED",
            "online": True,
            "observed_at": 1_001.0,
        }
        node["events"] = [
            {
                "sequence": 1,
                "type": "ingest.connected",
                "occurred_at": 1_001.0,
                "payload": {},
            }
        ]
        node["next_event_seq"] = 2
        node_internal.validate_node_authority(state)

        state["nodes"]["node-0001"]["ingest"]["online"] = "yes"
        with self.assertRaises(node_internal.NodeStateError):
            node_internal.validate_node_authority(state)

        state = valid_authority()
        state["nodes"]["node-0001"]["events"] = [
            {
                "sequence": 1,
                "type": "ingest.connected",
                "occurred_at": math.nan,
                "payload": {},
            }
        ]
        with self.assertRaises(node_internal.NodeStateError):
            node_internal.validate_node_authority(state)

    def test_huge_token_timestamp_is_controlled_state_error(self) -> None:
        state = valid_authority()
        state["tokens"]["a" * 64] = {
            "consumed": True,
            "consumed_at": 10**1000,
            "node_id": "node-0001",
            "session_id": "session-1",
        }
        with self.assertRaises(node_internal.NodeStateError):
            node_internal.validate_node_authority(state)

    def test_nonfinite_json_constants_are_rejected_on_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nodes.json"
            text = json.dumps(valid_authority()).replace("2000.0", "NaN", 1)
            path.write_text(text, encoding="utf-8")
            with self.assertRaises(node_internal.NodeStateError):
                node_internal.read_node_authority_snapshot(path)

    def test_invalid_write_does_not_replace_existing_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nodes.json"
            baseline = json.dumps(valid_authority(), sort_keys=True)
            path.write_text(baseline, encoding="utf-8")
            state = valid_authority()
            state["nodes"]["node-0001"]["absolute_deadline"] = math.inf
            with self.assertRaises(node_internal.NodeStateError):
                node_internal._write_authority(state)
            self.assertEqual(path.read_text(encoding="utf-8"), baseline)

    def test_request_models_reject_nonfinite_numbers_before_state_mutation(self) -> None:
        with self.assertRaises(ValidationError):
            node_internal.HeartbeatRequest(memory_mb=math.inf)
        with self.assertRaises(ValidationError):
            node_internal.IngestObservationRequest(
                status="ACCEPTED",
                path="live/input",
                online=True,
                observed_at=math.nan,
            )


if __name__ == "__main__":
    unittest.main()
