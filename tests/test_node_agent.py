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

from agent import (  # noqa: E402
    ControlPlaneHTTPError,
    ControlPlaneUnavailable,
    NodeAgent,
    http_json,
)
from supervisor import (  # noqa: E402
    ComposeSupervisor,
    FakeSupervisor,
    SupervisionResult,
)


class FakeControlPlane:
    """Minimal in-process stand-in for the Control Plane internal API."""

    def __init__(self) -> None:
        self.desired_state = "RUNNING"
        self.heartbeat_count = 0
        self.last_heartbeat: dict[str, object] | None = None
        self.egress_url = "rtmp://fake-egress/output/relay"
        self.egress_verified_peer_ip = "198.51.100.10"
        self.egress_mode = "DIRECT_PUSH"
        self.bootstrap_token = "test-bootstrap-token"
        self.node_access_token = ""
        self.node_id: str | None = None
        self.session_id: str | None = None
        self.bootstrap_request_id: str | None = None
        self.absolute_deadline = 10**12

    def run(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_factory())
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=3.0)
        self.server.server_close()

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
                    supplied_request_id = str(payload.get("bootstrap_request_id", ""))
                    supplied_access_token = str(payload.get("node_access_token", ""))
                    if control.bootstrap_request_id is None:
                        control.bootstrap_request_id = supplied_request_id
                        control.node_access_token = supplied_access_token
                        control.node_id = f"node-{uuid.uuid4().hex[:8]}"
                        control.session_id = str(uuid.uuid4())
                    elif (
                        supplied_request_id != control.bootstrap_request_id
                        or supplied_access_token != control.node_access_token
                    ):
                        self._send({"error": "consumed"}, status=409)
                        return
                    self._send(
                        {
                            "node_id": control.node_id,
                            "session_id": control.session_id,
                            "status": "BOOTSTRAPPING",
                            "absolute_deadline": control.absolute_deadline,
                            "egress_url": control.egress_url,
                            "egress_verified_peer_ip": control.egress_verified_peer_ip,
                            "egress_mode": control.egress_mode,
                            "media_mtx_config_ref": "config/mediamtx.yml",
                            "node_access_token": supplied_access_token,
                        }
                    )
                    return

                if self.path.endswith("/heartbeat"):
                    auth = self.headers.get("Authorization", "")
                    if auth != f"Bearer {control.node_access_token}":
                        self._send({"error": "unauthorized"}, status=401)
                        return
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
        self.assertEqual(
            (self.secret_dir / "egress_verified_peer_ip").read_text(encoding="utf-8").strip(),
            "198.51.100.10",
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

    def test_direct_push_requires_verified_peer_ip(self) -> None:
        response = self.agent.bootstrap()
        response.pop("egress_verified_peer_ip", None)
        with self.assertRaisesRegex(RuntimeError, "verified destination peer IP"):
            self.agent.write_secret(response)

    def test_seed_control_state_is_create_only(self) -> None:
        control_path = self.secret_dir / "control.json"
        self.agent.control_state_path = control_path
        self.agent.seed_control_state({"audio_mode": "MUTED", "audio_version": 2})
        self.assertEqual(json.loads(control_path.read_text())["audio_mode"], "MUTED")
        control_path.write_text(json.dumps({"audio_mode": "LIVE", "version": 9}))
        self.agent.seed_control_state({"audio_mode": "MUTED", "audio_version": 1})
        self.assertEqual(json.loads(control_path.read_text())["audio_mode"], "LIVE")

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
        with self.assertRaises(ControlPlaneUnavailable) as failure:
            http_json(
                f"http://127.0.0.1:{port}/unavailable",
                method="POST",
                payload={},
                timeout=0.2,
            )
        self.assertIn("control plane unavailable", str(failure.exception))

    def test_bootstrap_retries_transport_failure_without_rotating_secret(self) -> None:
        original_bootstrap = self.agent.bootstrap
        attempts = 0

        def flaky_bootstrap() -> dict[str, object]:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                # The Control Plane committed and replied, but the response
                # was lost after receipt. The exact attempt must be retryable.
                original_bootstrap()
                raise ControlPlaneUnavailable("control plane unavailable: starting")
            return original_bootstrap()

        self.control.desired_state = "STOPPED"
        self.agent.bootstrap_timeout_seconds = 1.0
        self.agent.bootstrap_retry_seconds = 0.05
        with patch.object(self.agent, "bootstrap", side_effect=flaky_bootstrap):
            self.assertEqual(self.agent.run(), 0)

        self.assertEqual(attempts, 2)
        self.assertFalse((self.secret_dir / "egress_url").exists())

    def test_bootstrap_does_not_retry_definitive_http_error(self) -> None:
        self.agent.bootstrap_timeout_seconds = 1.0
        with patch.object(
            self.agent,
            "bootstrap",
            side_effect=ControlPlaneHTTPError(401, "denied"),
        ) as bootstrap:
            with self.assertRaises(ControlPlaneHTTPError):
                self.agent.bootstrap_with_retry()
        bootstrap.assert_called_once_with()

    def test_run_stops_media_and_cleans_lifecycle_on_bootstrap_denial(self) -> None:
        cleanup_calls: list[str] = []
        self.agent.post_media_stop = lambda: cleanup_calls.append("cleanup")
        with patch.object(
            self.agent,
            "bootstrap_with_retry",
            side_effect=ControlPlaneHTTPError(401, "denied"),
        ), self.assertRaises(ControlPlaneHTTPError):
            self.agent.run()

        self.assertEqual(self.supervisor.stopped_sessions, ["unknown"])
        self.assertEqual(cleanup_calls, ["cleanup"])
        self.assertFalse((self.secret_dir / "egress_url").exists())

    def test_run_stops_media_when_external_secret_write_fails(self) -> None:
        cleanup_calls: list[str] = []
        self.agent.post_media_stop = lambda: cleanup_calls.append("cleanup")
        with patch.object(
            self.agent,
            "write_secret",
            side_effect=RuntimeError("secret write failed"),
        ), self.assertRaisesRegex(RuntimeError, "secret write failed"):
            self.agent.run()

        self.assertEqual(self.supervisor.stopped_sessions, [self.agent.session_id])
        self.assertEqual(cleanup_calls, ["cleanup"])

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

    def test_deadline_watchdog_stops_media_while_heartbeat_is_blocked(self) -> None:
        health_entered = threading.Event()
        release_health = threading.Event()

        class BlockingSupervisor(FakeSupervisor):
            def health(self) -> dict[str, object]:
                health_entered.set()
                release_health.wait(timeout=5)
                return super().health()

        supervisor = BlockingSupervisor()
        self.agent.supervisor = supervisor
        # Do not let scheduler/runner load expire the bootstrap deadline before
        # the test has actually entered the blocked heartbeat path. Arm the real
        # watchdog only after health() proves that the heartbeat is blocked.
        self.control.absolute_deadline = time.time() + 30.0
        result: list[int] = []
        thread = threading.Thread(target=lambda: result.append(self.agent.run()), daemon=True)
        thread.start()
        self.assertTrue(health_entered.wait(timeout=10))
        self.agent.absolute_deadline = time.time() + 0.05

        deadline = time.monotonic() + 1.0
        while not supervisor.stopped_sessions and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(supervisor.stopped_sessions, [self.agent.session_id])

        release_health.set()
        thread.join(timeout=3)
        self.assertEqual(result, [0])
        self.assertEqual(len(supervisor.stopped_sessions), 1)

    def test_expired_deadline_refuses_media_start(self) -> None:
        self.control.absolute_deadline = time.time() - 1.0

        self.assertEqual(self.agent.run(), 1)

        self.assertEqual(self.supervisor.started_sessions, [])
        self.assertEqual(self.supervisor.stopped_sessions, [self.agent.session_id])
        self.assertFalse((self.secret_dir / "egress_url").exists())

    def test_deadline_watchdog_stops_media_while_start_is_blocked(self) -> None:
        start_entered = threading.Event()
        release_start = threading.Event()

        class BlockingStartSupervisor(FakeSupervisor):
            def start(
                self,
                session_id: str,
                *,
                egress_mode: str = "DIRECT_PUSH",
            ) -> SupervisionResult:
                result = super().start(session_id, egress_mode=egress_mode)
                start_entered.set()
                release_start.wait(timeout=5)
                return result

        supervisor = BlockingStartSupervisor()
        self.agent.supervisor = supervisor
        # Do not let scheduler/runner load expire the bootstrap deadline before
        # the test has actually entered the blocked supervisor start path.
        self.control.absolute_deadline = time.time() + 30.0
        result: list[int] = []
        thread = threading.Thread(target=lambda: result.append(self.agent.run()), daemon=True)
        thread.start()
        self.assertTrue(start_entered.wait(timeout=10))

        # Arm the real watchdog only after start() is known to be blocked. This
        # keeps the safety assertion while removing the pre-start wall-clock race.
        self.agent.absolute_deadline = time.time() + 0.05
        deadline = time.monotonic() + 1.0
        while not supervisor.stopped_sessions and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(supervisor.stopped_sessions, [self.agent.session_id])

        release_start.set()
        thread.join(timeout=3)
        self.assertEqual(result, [0])
        self.assertEqual(len(supervisor.started_sessions), 1)
        self.assertEqual(len(supervisor.stopped_sessions), 1)

    def test_signal_stops_media_while_start_is_blocked(self) -> None:
        start_entered = threading.Event()
        release_start = threading.Event()

        class BlockingStartSupervisor(FakeSupervisor):
            def start(
                self,
                session_id: str,
                *,
                egress_mode: str = "DIRECT_PUSH",
            ) -> SupervisionResult:
                result = super().start(session_id, egress_mode=egress_mode)
                start_entered.set()
                release_start.wait(timeout=5)
                return result

        supervisor = BlockingStartSupervisor()
        self.agent.supervisor = supervisor
        result: list[int] = []
        thread = threading.Thread(target=lambda: result.append(self.agent.run()), daemon=True)
        thread.start()
        self.assertTrue(start_entered.wait(timeout=3))
        self.agent.handle_signal(15, None)

        deadline = time.monotonic() + 1.0
        while not supervisor.stopped_sessions and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(supervisor.stopped_sessions, [self.agent.session_id])

        release_start.set()
        thread.join(timeout=3)
        self.assertEqual(result, [0])
        self.assertEqual(len(supervisor.started_sessions), 1)
        self.assertEqual(len(supervisor.stopped_sessions), 1)

    def test_signal_stops_media_while_heartbeat_is_blocked(self) -> None:
        health_entered = threading.Event()
        release_health = threading.Event()

        class BlockingSupervisor(FakeSupervisor):
            def health(self) -> dict[str, object]:
                health_entered.set()
                release_health.wait(timeout=5)
                return super().health()

        supervisor = BlockingSupervisor()
        self.agent.supervisor = supervisor
        result: list[int] = []
        thread = threading.Thread(target=lambda: result.append(self.agent.run()), daemon=True)
        thread.start()
        self.assertTrue(health_entered.wait(timeout=3))
        self.agent.handle_signal(15, None)

        deadline = time.monotonic() + 1.0
        while not supervisor.stopped_sessions and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(supervisor.stopped_sessions, [self.agent.session_id])

        release_health.set()
        thread.join(timeout=3)
        self.assertEqual(result, [0])
        self.assertEqual(len(supervisor.stopped_sessions), 1)

    def test_definitive_heartbeat_http_errors_stop_immediately(self) -> None:
        for status in (401, 403, 404, 409):
            with self.subTest(status=status):
                supervisor = FakeSupervisor()
                self.agent.supervisor = supervisor
                self.agent._shutdown_event.clear()
                self.agent._stop_requested = False
                self.agent._media_stop_attempted = False
                self.agent._media_stop_result = None
                self.agent._media_stop_error = None
                with patch.object(
                    self.agent,
                    "heartbeat",
                    side_effect=ControlPlaneHTTPError(status, "denied"),
                ) as heartbeat:
                    self.assertEqual(self.agent.run(), 0)
                heartbeat.assert_called_once_with()
                self.assertEqual(len(supervisor.stopped_sessions), 1)

    def test_transient_heartbeat_http_error_is_retried(self) -> None:
        self.agent.heartbeat_interval = 0.01
        with patch.object(
            self.agent,
            "heartbeat",
            side_effect=[
                ControlPlaneHTTPError(503, "starting"),
                {"desired_state": "STOPPED"},
            ],
        ) as heartbeat:
            self.assertEqual(self.agent.run(), 0)
        self.assertEqual(heartbeat.call_count, 2)

    def test_run_removes_secret_when_supervisor_start_raises(self) -> None:
        class RaisingSupervisor(FakeSupervisor):
            def start(
                self,
                session_id: str,
                *,
                egress_mode: str = "DIRECT_PUSH",
            ) -> SupervisionResult:
                raise RuntimeError("start failed")

        supervisor = RaisingSupervisor()
        self.agent.supervisor = supervisor

        self.assertEqual(self.agent.run(), 1)
        self.assertEqual(supervisor.stopped_sessions, [self.agent.session_id])
        self.assertFalse((self.secret_dir / "egress_url").exists())


class ComposeSupervisorTest(unittest.TestCase):
    def _supervisor(self) -> ComposeSupervisor:
        return ComposeSupervisor(
            compose_file=ROOT / "docker-compose.node.yml",
            startup_timeout_seconds=0,
        )

    @staticmethod
    def _successful_compose(supervisor, calls):
        def fake_compose(*args: str):
            calls.append(list(args))
            if args[0] == "ps":
                output = "\n".join(
                    json.dumps({"Service": service, "State": "running"})
                    for service in supervisor.required_services
                )
                return SimpleNamespace(returncode=0, stdout=output, stderr="")
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")

        return fake_compose

    def test_disabled_egress_gateway_is_stopped_and_not_started(self) -> None:
        supervisor = self._supervisor()
        calls: list[list[str]] = []

        with patch.dict("os.environ", {"EGRESS_GATEWAY_ENABLED": "0"}), patch.object(
            supervisor,
            "_compose",
            side_effect=self._successful_compose(supervisor, calls),
        ):
            result = supervisor.start("session-relay-only")

        self.assertTrue(result.ok)
        self.assertIn(["stop", "egress-gateway"], calls)
        self.assertIn(["start", "mediamtx", "continuity"], calls)

    def test_enabled_egress_gateway_starts_only_media_services(self) -> None:
        supervisor = self._supervisor()
        calls: list[list[str]] = []

        with patch.dict("os.environ", {"EGRESS_GATEWAY_ENABLED": "1"}), patch.object(
            supervisor,
            "_compose",
            side_effect=self._successful_compose(supervisor, calls),
        ):
            result = supervisor.start("session-direct-push")

        self.assertTrue(result.ok)
        self.assertIn(
            ["start", "mediamtx", "continuity", "egress-gateway"], calls
        )
        self.assertFalse(any("node-agent" in call for call in calls))

    def test_relay_only_mode_disables_gateway_even_when_env_enabled(self) -> None:
        supervisor = self._supervisor()
        calls: list[list[str]] = []

        with patch.dict("os.environ", {"EGRESS_GATEWAY_ENABLED": "1"}), patch.object(
            supervisor,
            "_compose",
            side_effect=self._successful_compose(supervisor, calls),
        ):
            result = supervisor.start(
                "session-relay-only",
                egress_mode="RELAY_ONLY",
            )

        self.assertTrue(result.ok)
        self.assertIn(["stop", "egress-gateway"], calls)
        self.assertIn(["start", "mediamtx", "continuity"], calls)

    def test_health_accepts_official_lowercase_running_json_lines(self) -> None:
        supervisor = self._supervisor()
        supervisor.required_services = ("mediamtx", "continuity")
        output = (
            '{"Service":"mediamtx","State":"running"}\n'
            '{"Service":"continuity","State":"running"}'
        )
        with patch.object(
            supervisor,
            "_compose",
            return_value=SimpleNamespace(returncode=0, stdout=output, stderr=""),
        ):
            self.assertEqual(
                supervisor.health(),
                {"media_stack": "running", "compose_ok": True},
            )

    def test_health_reports_stopped_when_required_service_exited(self) -> None:
        supervisor = self._supervisor()
        supervisor.required_services = ("mediamtx", "continuity")
        output = (
            '{"Service":"mediamtx","State":"running"}\n'
            '{"Service":"continuity","State":"exited"}'
        )
        with patch.object(
            supervisor,
            "_compose",
            return_value=SimpleNamespace(returncode=0, stdout=output, stderr=""),
        ):
            self.assertEqual(supervisor.health()["media_stack"], "stopped")

    def test_health_reports_unknown_for_malformed_compose_output(self) -> None:
        supervisor = self._supervisor()
        with patch.object(
            supervisor,
            "_compose",
            return_value=SimpleNamespace(returncode=0, stdout="{broken", stderr=""),
        ):
            self.assertEqual(
                supervisor.health(),
                {"media_stack": "unknown", "compose_ok": False},
            )

    def test_missing_compose_file_fails_before_start(self) -> None:
        supervisor = ComposeSupervisor(
            compose_file=ROOT / "missing-compose.yml",
            startup_timeout_seconds=0,
        )
        result = supervisor.start("session-direct-push")
        self.assertFalse(result.ok)
        self.assertIn("missing", result.detail)

    def test_stop_targets_media_services_without_downing_agent(self) -> None:
        supervisor = self._supervisor()
        calls: list[list[str]] = []
        with patch.object(
            supervisor,
            "_compose",
            side_effect=self._successful_compose(supervisor, calls),
        ):
            result = supervisor.stop("session-direct-push")
        self.assertTrue(result.ok)
        self.assertFalse(any(call[0] == "down" for call in calls))
        self.assertFalse(any("node-agent" in call for call in calls))


if __name__ == "__main__":
    unittest.main()
