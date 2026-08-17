from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "node-agent"))

from agent import NodeAgent  # noqa: E402
from supervisor import FakeSupervisor  # noqa: E402


class FakeControlPlane:
    """Minimal in-process stand-in for the Control Plane internal API."""

    def __init__(self) -> None:
        self.desired_state = "RUNNING"
        self.heartbeat_count = 0
        self.egress_url = "rtmp://fake-egress/output/relay"
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
                            "media_mtx_config_ref": "config/mediamtx.yml",
                        }
                    )
                    return

                if self.path.endswith("/heartbeat"):
                    control.heartbeat_count += 1
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
        self.assertEqual(secret_path.read_text(encoding="utf-8").strip(), "rtmp://fake-egress/output/relay")
        mode = secret_path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)
        self.assertEqual(self.agent.node_id, self.control.node_id)
        self.assertIsNotNone(self.agent.session_id)

    def test_heartbeat_reports_ready_and_receives_desired_state(self) -> None:
        response = self.agent.bootstrap()
        self.agent.write_secret(response)
        self.supervisor.start("session-test")
        heartbeat = self.agent.heartbeat()
        self.assertEqual(heartbeat["desired_state"], "RUNNING")
        self.assertEqual(self.control.heartbeat_count, 1)

    def test_run_starts_media_and_stops_on_signal(self) -> None:
        response = self.agent.bootstrap()
        self.agent.write_secret(response)
        # Run the loop in a thread so we can send SIGTERM after bootstrapping.
        import signal as signal_module

        result: list[int] = []

        def run_agent() -> None:
            # The agent installs signal handlers; simulate via the STOP path by
            # flipping desired_state to STOPPED from the control plane.
            self.control.desired_state = "STOPPED"
            result.append(self.agent.run())

        thread = threading.Thread(target=run_agent, daemon=True)
        thread.start()
        thread.join(timeout=10.0)
        self.assertEqual(result, [0])
        session_id = self.agent.session_id
        self.assertIsNotNone(session_id)
        self.assertIn(session_id, self.supervisor.started_sessions)
        self.assertIn(session_id, self.supervisor.stopped_sessions)
        self.assertFalse(self.supervisor.running)


if __name__ == "__main__":
    unittest.main()
