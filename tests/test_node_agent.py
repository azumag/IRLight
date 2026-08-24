from __future__ import annotations

import json
import os
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "node-agent"))

from agent import NodeAgent, http_json  # noqa: E402
from supervisor import ComposeSupervisor, FakeSupervisor  # noqa: E402


class FakeControlPlane:
    """Minimal in-process stand-in for the Control Plane internal API."""

    def __init__(self) -> None:
        self.desired_state = "RUNNING"
        self.heartbeat_count = 0
        self.last_heartbeat: dict[str, object] | None = None
        self.egress_url = "rtmp://fake-egress/output/relay"
        self.egress_mode = "DIRECT_PUSH"
        self.bootstrap_token = "test-bootstrap-token"

    def run(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_factory())
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=3.0)

    def _handler_factory(self):
        control = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def _send(self, payload: dict[str, object], status: int = 200) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    payload = {}

                if self.path == "/internal/nodes/bootstrap":
                    auth = self.headers.get("Authorization", "")
                    if not auth.endswith(control.bootstrap_token):
                        self._send({"error": "unauthorized"}, status=401)
                        return
                    control.node_id = f"node-{uuid.uuid4().hex[:8]}"
                    self._send(
                        {
                            "node_id": control.node_id,
                            "session_id": str(uuid.uuid4()),
                            "status": "BOOTSTRAPPING",
                            "absolute_deadline": 10**12,
                            "egress_url": control.egress_url,
                            "egress_mode": control.egress_mode,
                            "media_mtx_config_ref": "config/mediamtx.yml",
                        }
                    )
                    return

                if self.path.endswith("/heartbeat"):
                    control.heartbeat_count += 1
                    control.last_heartbeat = payload if isinstance(payload, dict) else {}
                    self._send({"desired_state": control.desired_state})
                    return
                self._send({"error": "not found"}, status=404)

        return Handler


class NodeAgentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.control = FakeControlPlane()
        self.control.run()
        self.secret_dir = Path(tempfile.mkdtemp(prefix="irlight-agent-secrets-"))
        self.supervisor = FakeSupervisor()
        self.agent = NodeAgent(
            control_base_url=f"http://127.0.0.1:{self.control.port}",
            bootstrap_token=self.control.bootstrap_token,
            provider_server_id="conoha-test-1",
            boot_id="boot-test",
            agent_version="0.2.0-spike",
            secret_dir=self.secret_dir,
            supervisor=self.supervisor,
            heartbeat_interval=0.05,
        )

    def tearDown(self) -> None:
        self.control.stop()

    def test_bootstrap_writes_secret_to_tmpfs_0600(self) -> None:
        response = self.agent.bootstrap()
        secret_path = self.agent.write_secret(response)
        self.assertTrue(secret_path.exists())
        self.assertEqual(
            secret_path.read_text(encoding="utf-8").strip(),
            "rtmp://fake-egress/output/relay",
        )
        mode = secret_path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)
        self.assertEqual(self.secret_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.agent.node_id, self.control.node_id)
        self.assertIsNotNone(self.agent.session_id)

    def test_relay_only_clears_stale_secrets_and_ignores_status(self) -> None:
        self.control.egress_mode = "RELAY_ONLY"
        stale_secret = self.secret_dir / "egress_url"
        stale_peer = self.secret_dir / "egress_verified_peer_ip"
        stale_secret.write_text("rtmp://stale/output/relay", encoding="utf-8")
        stale_peer.write_text("192.0.2.1", encoding="utf-8")
        status_path = self.secret_dir.parent / f"egress-status-{uuid.uuid4().hex}.json"
        status_path.write_text(
            json.dumps({"status": "CONNECTED", "connected": True, "observed_at": time.time()}),
            encoding="utf-8",
        )

        response = self.agent.bootstrap()
        secret_path = self.agent.write_secret(response)
        self.agent.egress_status_file = status_path

        self.assertIsNone(secret_path)
        self.assertFalse(stale_secret.exists())
        self.assertFalse(stale_peer.exists())
        self.assertEqual(self.agent.egress_mode, "RELAY_ONLY")
        self.assertIsNone(self.agent._egress_observation())

    def test_heartbeat_reports_ready_and_receives_desired_state(self) -> None:
        response = self.agent.bootstrap()
        self.agent.write_secret(response)
        self.supervisor.start("session-test")
        heartbeat = self.agent.heartbeat()
        self.assertEqual(heartbeat["desired_state"], "RUNNING")
        self.assertEqual(self.control.heartbeat_count, 1)

    def test_heartbeat_reports_safe_egress_status(self) -> None:
        status_path = self.secret_dir.parent / f"egress-status-{uuid.uuid4().hex}.json"
        status_path.write_text(
            json.dumps(
                {
                    "status": "RECONNECTING",
                    "connected": False,
                    "attempt": 3,
                    "reason_code": "UNREACHABLE",
                    "rendered_buffers": 0,
                    "destination_scheme": "rtmps",
                    "destination_host": "live.example",
                    "observed_at": time.time(),
                    "stream_key": "must-not-leak",
                }
            ),
            encoding="utf-8",
        )
        self.agent.egress_status_file = status_path
        response = self.agent.bootstrap()
        self.agent.write_secret(response)
        self.supervisor.start("session-test")
        self.agent.heartbeat()
        payload = self.control.last_heartbeat or {}
        self.assertFalse(payload.get("egress_connected"))
        egress = payload.get("egress")
        self.assertIsInstance(egress, dict)
        self.assertEqual(egress.get("status"), "RECONNECTING")
        self.assertEqual(egress.get("reason_code"), "UNREACHABLE")
        self.assertNotIn("stream_key", egress)
        self.assertNotIn("must-not-leak", str(payload))

    def test_relay_only_heartbeat_reports_anonymous_client_state(self) -> None:
        self.control.egress_mode = "RELAY_ONLY"
        status_path = self.secret_dir.parent / f"direct-egress-{uuid.uuid4().hex}.json"
        status_path.write_text(
            json.dumps({"status": "CONNECTED", "connected": True, "observed_at": time.time()}),
            encoding="utf-8",
        )

        class Observer:
            def observe(self):
                return {
                    "status": "CONNECTED",
                    "connected": True,
                    "reader_count": 1,
                    "reason_code": None,
                    "observed_at": 123.0,
                }

        self.agent.relay_client_observer = Observer()
        self.agent.egress_status_file = status_path
        response = self.agent.bootstrap()
        self.agent.write_secret(response)
        self.supervisor.start("session-test")
        self.agent.heartbeat()

        payload = self.control.last_heartbeat or {}
        self.assertIsNone(payload.get("egress"))
        relay = payload.get("relay_client")
        self.assertIsInstance(relay, dict)
        assert isinstance(relay, dict)
        self.assertTrue(relay["connected"])
        self.assertEqual(relay["reader_count"], 1)
        self.assertNotIn("client_id", relay)

    def test_transport_failure_is_wrapped_as_runtime_error(self) -> None:
        unused = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        port = int(unused.server_address[1])
        unused.server_close()
        with self.assertRaises(RuntimeError) as failure:
            http_json(
                f"http://127.0.0.1:{port}/unavailable",
                method="POST",
                payload={},
                timeout=0.2,
            )
        self.assertIn("control plane unavailable", str(failure.exception))

    def test_run_starts_media_stops_and_removes_secret(self) -> None:
        result: list[int] = []

        def run_agent() -> None:
            self.control.desired_state = "STOPPED"
            result.append(self.agent.run())

        thread = threading.Thread(target=run_agent, daemon=True)
        thread.start()
        thread.join(timeout=10.0)
        self.assertEqual(result, [0])
        session_id = self.agent.session_id
        self.assertIsNotNone(session_id)
        self.assertIn(session_id, self.supervisor.started_sessions)
        self.assertEqual(self.supervisor.started_egress_modes, ["DIRECT_PUSH"])
        self.assertIn(session_id, self.supervisor.stopped_sessions)
        self.assertFalse(self.supervisor.running)
        self.assertFalse((self.secret_dir / "egress_url").exists())


class ComposeSupervisorTest(unittest.TestCase):
    def test_disabled_egress_gateway_is_scaled_to_zero(self) -> None:
        supervisor = ComposeSupervisor(compose_file="docker-compose.node.yml")
        calls: list[list[str]] = []

        def fake_compose(*args: str):
            calls.append(list(args))
            output = "running" if args[0] == "ps" else "started"
            if args[0] == "ps":
                return SimpleNamespace(
                    returncode=0,
                    stdout=f'{{"State":"{output}"}}',
                    stderr="",
                )
            return SimpleNamespace(returncode=0, stdout=output, stderr="")

        with patch.dict("os.environ", {"EGRESS_GATEWAY_ENABLED": "0"}), patch.object(
            supervisor,
            "_compose",
            side_effect=fake_compose,
        ):
            result = supervisor.start("session-relay-only")

        self.assertTrue(result.ok)
        self.assertEqual(calls[0], ["up", "-d", "--scale", "egress-gateway=0"])

    def test_enabled_egress_gateway_does_not_scale_services(self) -> None:
        supervisor = ComposeSupervisor(compose_file="docker-compose.node.yml")
        calls: list[list[str]] = []

        def fake_compose(*args: str):
            calls.append(list(args))
            output = "running" if args[0] == "ps" else "started"
            return SimpleNamespace(returncode=0, stdout=output, stderr="")

        with patch.dict("os.environ", {"EGRESS_GATEWAY_ENABLED": "1"}), patch.object(
            supervisor,
            "_compose",
            side_effect=fake_compose,
        ):
            result = supervisor.start("session-direct-push")

        self.assertTrue(result.ok)
        self.assertEqual(calls[0], ["up", "-d"])

    def test_relay_only_mode_disables_gateway_even_when_env_enabled(self) -> None:
        supervisor = ComposeSupervisor(compose_file="docker-compose.node.yml")
        calls: list[list[str]] = []

        def fake_compose(*args: str):
            calls.append(list(args))
            output = "running" if args[0] == "ps" else "started"
            return SimpleNamespace(returncode=0, stdout=output, stderr="")

        with patch.dict("os.environ", {"EGRESS_GATEWAY_ENABLED": "1"}), patch.object(
            supervisor,
            "_compose",
            side_effect=fake_compose,
        ):
            result = supervisor.start(
                "session-relay-only",
                egress_mode="RELAY_ONLY",
            )

        self.assertTrue(result.ok)
        self.assertEqual(calls[0], ["up", "-d", "--scale", "egress-gateway=0"])


if __name__ == "__main__":
    unittest.main()
