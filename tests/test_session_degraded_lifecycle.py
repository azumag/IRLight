from __future__ import annotations

import sys
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "provider"))
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from fake_provider import FakeProvider  # noqa: E402
from session_store import ACTIVE_STATES, SessionStore  # noqa: E402
from session_workflow import ProvisioningWorkflow, WorkflowConfig  # noqa: E402


class SessionDegradedLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SessionStore(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _ready(self) -> str:
        session = self.store.create(user_id="user-1", environment="dev")
        session_id = str(session["session_id"])
        self.store.transition(session_id, "PROVISIONING")
        self.store.transition(session_id, "BOOTSTRAPPING")
        self.store.transition(session_id, "READY_WAIT_INGEST")
        return session_id

    def test_degraded_is_a_formal_active_state(self) -> None:
        session_id = self._ready()
        self.store.transition(session_id, "DEGRADED")
        self.assertIn("DEGRADED", ACTIVE_STATES)
        self.assertEqual(self.store.get(session_id)["status"], "DEGRADED")

        self.store.transition(session_id, "LIVE")
        self.store.transition(session_id, "DEGRADED")
        self.store.transition(session_id, "HOLDING")
        self.store.transition(session_id, "DEGRADED")
        self.assertEqual(self.store.get(session_id)["status"], "DEGRADED")

    def test_manual_stop_wins_while_degraded(self) -> None:
        provider = FakeProvider()
        workflow = ProvisioningWorkflow(self.store, provider, WorkflowConfig())
        session_id = str(uuid.uuid4())
        workflow.prepare(session_id, user_id="user-1", environment="dev")
        self.store.transition(session_id, "DEGRADED")

        finished = workflow.stop(session_id)
        self.assertEqual(finished["status"], "FINISHED")
        self.assertEqual(provider.list_managed_resources(), [])

        late = self.store.apply_ingest_observation(
            session_id,
            node_id="node-1",
            event_types=["ingest.reconnected"],
            observation={
                "status": "ACCEPTED",
                "path": "live/input",
                "online": True,
                "source_type": "rtmpConn",
                "source_id": "late-source",
                "bitrate_bps": 1_500_000,
                "max_bitrate_bps": 6_000_000,
                "tracks": [],
                "quality": None,
                "reasons": [],
                "warnings": [],
                "enforced": False,
                "observed_at": 200.0,
            },
            occurred_at=200.0,
        )
        self.assertEqual(late["status"], "FINISHED")
        self.assertFalse(any(event["type"] == "ingest.reconnected" for event in late["events"]))


if __name__ == "__main__":
    unittest.main()
