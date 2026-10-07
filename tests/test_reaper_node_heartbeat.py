from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import unittest
import uuid
from collections.abc import Iterator
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

import node_internal  # noqa: E402
from provider.conoha import SessionMetadata  # noqa: E402
from provider.fake_provider import FakeProvider  # noqa: E402
from reaper import Reaper, ReaperConfig  # noqa: E402
from session_store import SessionStore  # noqa: E402


TEST_USER_ID = "11111111-1111-4111-8111-111111111111"


@contextlib.contextmanager
def node_authority_env(env: dict[str, str | None]) -> Iterator[None]:
    """Pin the state-root variables for one reaper default-resolution check.

    Only ``NODE_STATE_DIR`` and ``STATE_DIR`` are managed; a ``None`` value
    removes the variable for the duration of the block.
    """
    names = ("NODE_STATE_DIR", "STATE_DIR")
    previous = {name: os.environ.get(name) for name in names}
    try:
        for name in names:
            value = env.get(name)
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class NodeHeartbeatReaperTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = SessionStore(self.root / "sessions")
        self.nodes_path = self.root / "nodes.json"
        self.provider = FakeProvider()
        self.config = ReaperConfig(
            provisioning_timeout_seconds=10_000.0,
            no_ingest_timeout_seconds=10_000.0,
            hold_timeout_seconds=10_000.0,
            heartbeat_grace_seconds=120.0,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _session(self, *, registered_at: float = 100.0) -> tuple[str, str]:
        session_id = str(uuid.uuid4())
        provider_server_id = f"provider-{session_id}"
        self.store.create(
            session_id=session_id,
            user_id=TEST_USER_ID,
            environment="dev",
            absolute_deadline_hours=12.0,
        )
        self.store.transition(session_id, "PROVISIONING")
        self.store.transition(
            session_id,
            "BOOTSTRAPPING",
            provider_server_id=provider_server_id,
            provider_public_ipv4="198.51.100.41",
        )
        self.store.transition(session_id, "READY_WAIT_INGEST", ready_at=registered_at)
        self.store.bind_node(
            session_id,
            node_id="node-0001",
            boot_id="boot-heartbeat-reaper",
            provider_server_id=provider_server_id,
            registered_at=registered_at,
        )

        tags = SessionMetadata(
            session_id=session_id,
            user_id=TEST_USER_ID,
            environment="dev",
        ).as_tags()
        volume = self.provider.create_volume("boot", 20, tags)
        self.provider.create_server(
            "node",
            image_ref="ubuntu-24.04",
            flavor_ref="g2",
            volume_id=volume.volume_id,
            metadata=tags,
        )
        return session_id, provider_server_id

    def _write_nodes(
        self,
        *,
        session_id: str,
        last_heartbeat_at: float | None,
        node_session_id: str | None = None,
    ) -> None:
        self.nodes_path.write_text(
            json.dumps(
                {
                    "nodes": {
                        "node-0001": {
                            "node_id": "node-0001",
                            "session_id": node_session_id or session_id,
                            "provider_server_id": "provider-test",
                            "boot_id": "boot-heartbeat-reaper",
                            "agent_version": "test",
                            "status": "READY",
                            "desired_state": "RUNNING",
                            "absolute_deadline": 50_000.0,
                            "last_heartbeat_at": last_heartbeat_at,
                            "created_at": 1.0,
                            "access_token_sha256": "0" * 64,
                        }
                    },
                    "next_node_seq": 2,
                    "tokens": {},
                }
            ),
            encoding="utf-8",
        )

    def _reaper(self, *, now: float) -> Reaper:
        return Reaper(
            self.store,
            self.provider,
            self.config,
            now=now,
            node_state_path=self.nodes_path,
        )

    def test_recent_heartbeat_stays_active_until_exact_grace_boundary(self) -> None:
        session_id, _ = self._session()
        self._write_nodes(session_id=session_id, last_heartbeat_at=100.0)

        result = self._reaper(now=219.999).run()
        self.assertEqual(result["heartbeat_failures"], 0)
        self.assertEqual(self.store.get(session_id)["status"], "READY_WAIT_INGEST")
        self.assertEqual(len(self.provider.list_managed_resources()), 2)

        result = self._reaper(now=220.0).run()
        self.assertEqual(result["heartbeat_failures"], 1)
        session = self.store.get(session_id)
        assert session is not None
        self.assertEqual(session["status"], "FAILED")
        self.assertFalse(session["cleanup_pending"])
        self.assertEqual(session["failure_reason_code"], "NODE_SHUTDOWN")
        self.assertEqual(self.provider.list_managed_resources(), [])
        self.assertEqual(session["events"][-2]["type"], "session.failure_detected")
        self.assertEqual(session["events"][-2]["reason_code"], "NODE_SHUTDOWN")
        self.assertEqual(session["events"][-2]["payload"]["last_heartbeat_at"], 100.0)
        self.assertEqual(session["events"][-1]["type"], "session.failed")
        self.assertEqual(session["events"][-1]["reason_code"], "NODE_SHUTDOWN")

    def test_no_first_heartbeat_uses_node_registration_time(self) -> None:
        session_id, _ = self._session(registered_at=50.0)
        self._write_nodes(session_id=session_id, last_heartbeat_at=None)

        result = self._reaper(now=169.999).run()
        self.assertEqual(result["heartbeat_failures"], 0)
        self.assertEqual(self.store.get(session_id)["status"], "READY_WAIT_INGEST")

        result = self._reaper(now=170.0).run()
        self.assertEqual(result["heartbeat_failures"], 1)
        session = self.store.get(session_id)
        assert session is not None
        detected = next(
            event
            for event in session["events"]
            if event.get("type") == "session.failure_detected"
        )
        self.assertIsNone(detected["payload"]["last_heartbeat_at"])
        self.assertEqual(detected["payload"]["node_registered_at"], 50.0)

    def test_epoch_zero_heartbeat_is_not_replaced_by_registration_time(self) -> None:
        session_id, _ = self._session(registered_at=100.0)
        self._write_nodes(session_id=session_id, last_heartbeat_at=0.0)

        result = self._reaper(now=120.0).run()

        self.assertEqual(result["heartbeat_failures"], 1)
        session = self.store.get(session_id)
        assert session is not None
        detected = next(
            event
            for event in session["events"]
            if event.get("type") == "session.failure_detected"
        )
        self.assertEqual(detected["payload"]["last_heartbeat_at"], 0.0)

    def test_missing_node_record_uses_registration_time(self) -> None:
        session_id, _ = self._session(registered_at=80.0)
        self.nodes_path.write_text(
            json.dumps({"nodes": {}, "next_node_seq": 1, "tokens": {}}),
            encoding="utf-8",
        )

        result = self._reaper(now=200.0).run()
        self.assertEqual(result["heartbeat_failures"], 1)
        self.assertEqual(self.store.get(session_id)["status"], "FAILED")

    def test_corrupt_or_missing_node_registry_is_fail_safe(self) -> None:
        session_id, _ = self._session(registered_at=10.0)

        # Missing registry is not evidence that all Nodes disappeared.
        result = self._reaper(now=1_000.0).run()
        self.assertEqual(result["heartbeat_failures"], 0)
        self.assertEqual(self.store.get(session_id)["status"], "READY_WAIT_INGEST")

        self.nodes_path.write_text("{broken", encoding="utf-8")
        result = self._reaper(now=1_000.0).run()
        self.assertEqual(result["heartbeat_failures"], 0)
        self.assertEqual(self.store.get(session_id)["status"], "READY_WAIT_INGEST")
        self.assertEqual(len(self.provider.list_managed_resources()), 2)

    def test_structurally_corrupt_node_registry_skips_heartbeat_enforcement(self) -> None:
        session_id, _ = self._session(registered_at=10.0)
        self._write_nodes(session_id=session_id, last_heartbeat_at=0.0)
        payload = json.loads(self.nodes_path.read_text(encoding="utf-8"))
        for status in ("INVALID", [], {}):
            with self.subTest(status=status):
                payload["nodes"]["node-0001"]["status"] = status
                self.nodes_path.write_text(json.dumps(payload), encoding="utf-8")
                before = self.nodes_path.read_bytes()

                result = self._reaper(now=1_000.0).run()
                self.assertEqual(result["heartbeat_failures"], 0)
                self.assertEqual(self.store.get(session_id)["status"], "READY_WAIT_INGEST")
                self.assertEqual(len(self.provider.list_managed_resources()), 2)
                self.assertEqual(self.nodes_path.read_bytes(), before)

    def test_future_heartbeat_does_not_fail_on_wall_clock_correction(self) -> None:
        session_id, _ = self._session(registered_at=100.0)
        self._write_nodes(session_id=session_id, last_heartbeat_at=500.0)

        result = self._reaper(now=450.0).run()
        self.assertEqual(result["heartbeat_failures"], 0)
        self.assertEqual(self.store.get(session_id)["status"], "READY_WAIT_INGEST")

    def test_terminal_or_stopping_session_is_not_reclassified(self) -> None:
        session_id, _ = self._session(registered_at=10.0)
        self._write_nodes(session_id=session_id, last_heartbeat_at=10.0)
        self.store.transition(
            session_id,
            "STOPPING",
            allow_from={"READY_WAIT_INGEST"},
        )

        result = self._reaper(now=1_000.0).run()
        self.assertEqual(result["heartbeat_failures"], 0)
        # STOPPING cleanup is retried by the same sweep, but heartbeat
        # detection must not relabel it NODE_SHUTDOWN.
        session = self.store.get(session_id)
        assert session is not None
        self.assertEqual(session["status"], "FINISHED")
        self.assertIsNone(session.get("failure_reason_code"))


class ReaperNodeStatePathTest(unittest.TestCase):
    """The reaper must read the same Node authority as the Control Plane.

    Node authority resolution across the Control Plane, the heartbeat
    inspection CLI, and the heartbeat/availability alert tooling is
    ``NODE_STATE_DIR`` -> ``STATE_DIR`` -> ``/state``. Sweeping Sessions from
    one state root while enforcing heartbeats from another can both disable
    detection and make the reaper act on a foreign Node generation.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state_dir = self.root / "control"
        self.state_dir.mkdir()
        self.store = SessionStore(self.state_dir)
        self.nodes_path = self.state_dir / "nodes.json"
        self.provider = FakeProvider()
        self.config = ReaperConfig(
            provisioning_timeout_seconds=10_000.0,
            no_ingest_timeout_seconds=10_000.0,
            hold_timeout_seconds=10_000.0,
            heartbeat_grace_seconds=120.0,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _reaper(self, *, now: float) -> Reaper:
        """Build a reaper without an explicit node path so the default applies."""
        return Reaper(self.store, self.provider, self.config, now=now)

    def _write_nodes(
        self,
        *,
        session_id: str,
        last_heartbeat_at: float | None,
        path: Path | None = None,
    ) -> Path:
        target = path or self.nodes_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    "nodes": {
                        "node-0001": {
                            "node_id": "node-0001",
                            "session_id": session_id,
                            "provider_server_id": "provider-test",
                            "boot_id": "boot-heartbeat-reaper",
                            "agent_version": "test",
                            "status": "READY",
                            "desired_state": "RUNNING",
                            "absolute_deadline": 50_000.0,
                            "last_heartbeat_at": last_heartbeat_at,
                            "created_at": 1.0,
                            "access_token_sha256": "0" * 64,
                        }
                    },
                    "next_node_seq": 2,
                    "tokens": {},
                }
            ),
            encoding="utf-8",
        )
        return target

    def _session(self, *, registered_at: float = 100.0) -> str:
        session_id = str(uuid.uuid4())
        provider_server_id = f"provider-{session_id}"
        self.store.create(
            session_id=session_id,
            user_id=TEST_USER_ID,
            environment="dev",
            absolute_deadline_hours=12.0,
        )
        self.store.transition(session_id, "PROVISIONING")
        self.store.transition(
            session_id,
            "BOOTSTRAPPING",
            provider_server_id=provider_server_id,
            provider_public_ipv4="198.51.100.41",
        )
        self.store.transition(session_id, "READY_WAIT_INGEST", ready_at=registered_at)
        self.store.bind_node(
            session_id,
            node_id="node-0001",
            boot_id="boot-heartbeat-reaper",
            provider_server_id=provider_server_id,
            registered_at=registered_at,
        )
        return session_id

    def test_default_node_state_path_matches_control_plane_resolution(self) -> None:
        cases = (
            (
                {"NODE_STATE_DIR": "/node-state", "STATE_DIR": "/cp-state"},
                "/node-state/nodes.json",
            ),
            ({"NODE_STATE_DIR": None, "STATE_DIR": "/cp-state"}, "/cp-state/nodes.json"),
            ({"NODE_STATE_DIR": None, "STATE_DIR": None}, "/state/nodes.json"),
        )
        for env, expected in cases:
            with self.subTest(env=env), node_authority_env(env):
                reaper = self._reaper(now=0.0)
                self.assertEqual(reaper.node_state_path, Path(expected))
                # The Control Plane resolves the same Node authority root.
                self.assertEqual(
                    reaper.node_state_path,
                    node_internal._state_dir() / "nodes.json",
                )
                self.assertEqual(
                    reaper.node_state_path.parent,
                    node_internal.NODES_PATH.parent,
                )

    def test_explicit_node_state_path_still_wins(self) -> None:
        explicit = self.root / "explicit-nodes.json"
        with node_authority_env(
            {"NODE_STATE_DIR": "/node-state", "STATE_DIR": "/cp-state"}
        ):
            reaper = Reaper(
                self.store,
                self.provider,
                self.config,
                now=0.0,
                node_state_path=explicit,
            )
        self.assertEqual(reaper.node_state_path, explicit)

    def test_state_dir_node_authority_is_enforced_without_node_state_dir(self) -> None:
        session_id = self._session()
        self._write_nodes(session_id=session_id, last_heartbeat_at=100.0)

        with node_authority_env({"NODE_STATE_DIR": None, "STATE_DIR": str(self.state_dir)}):
            result = self._reaper(now=220.0).run()

        self.assertEqual(result["heartbeat_failures"], 1)
        session = self.store.get(session_id)
        assert session is not None
        self.assertEqual(session["status"], "FAILED")
        self.assertEqual(session["failure_reason_code"], "NODE_SHUTDOWN")

    def test_node_state_dir_authority_is_enforced_when_it_differs(self) -> None:
        session_id = self._session()
        self._write_nodes(
            session_id=session_id,
            last_heartbeat_at=100.0,
            path=self.root / "node" / "nodes.json",
        )
        self.assertFalse(self.nodes_path.exists())

        with node_authority_env(
            {"NODE_STATE_DIR": str(self.root / "node"), "STATE_DIR": str(self.state_dir)}
        ):
            result = self._reaper(now=220.0).run()

        self.assertEqual(result["heartbeat_failures"], 1)


if __name__ == "__main__":
    unittest.main()
