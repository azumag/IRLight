"""Media process supervision for the IRLight Node Agent.

The Node Agent never passes secrets through process arguments. Deployment
mounts node-local internal media URI files and the external egress URI through
tmpfs; the supervisor only starts, stops and observes existing media services.
"""

from __future__ import annotations

import abc
import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SupervisionResult:
    ok: bool
    detail: str


class MediaSupervisor(abc.ABC):
    @abc.abstractmethod
    def start(
        self,
        session_id: str,
        *,
        egress_mode: str = "DIRECT_PUSH",
    ) -> SupervisionResult:
        """Bring the media stack up and return health status."""

    @abc.abstractmethod
    def stop(self, session_id: str) -> SupervisionResult:
        """Gracefully tear the media stack down."""

    @abc.abstractmethod
    def health(self) -> dict[str, object]:
        """Return short-lived media health used in heartbeats."""


class ComposeSupervisor(MediaSupervisor):
    """Runs the production compose stack (prebuilt images, no build:)."""

    def __init__(
        self,
        compose_file: str | os.PathLike[str] | None = None,
        project_name: str = "irlight-node",
        startup_timeout_seconds: float | None = None,
        poll_interval_seconds: float = 1.0,
        command_timeout_seconds: float | None = None,
    ) -> None:
        self.compose_file = Path(
            compose_file or os.getenv("NODE_COMPOSE_FILE", "docker-compose.node.yml")
        )
        self.project_name = os.getenv("NODE_COMPOSE_PROJECT", project_name)
        if startup_timeout_seconds is None:
            try:
                startup_timeout_seconds = float(
                    os.getenv("NODE_MEDIA_START_TIMEOUT_SECONDS", "30")
                )
            except ValueError:
                startup_timeout_seconds = 30.0
        self.startup_timeout_seconds = max(0.0, startup_timeout_seconds)
        self.poll_interval_seconds = max(0.01, poll_interval_seconds)
        if command_timeout_seconds is None:
            try:
                command_timeout_seconds = float(
                    os.getenv("NODE_DOCKER_COMMAND_TIMEOUT_SECONDS", "15")
                )
            except ValueError:
                command_timeout_seconds = 15.0
        self.command_timeout_seconds = max(1.0, command_timeout_seconds)
        self.required_services = self._services_for_mode(
            os.getenv("NODE_EGRESS_MODE", "DIRECT_PUSH")
        )

    def _compose(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["COMPOSE_PROJECT_NAME"] = self.project_name
        command = ["docker", "compose", "-f", str(self.compose_file), *args]
        try:
            return subprocess.run(
                command,
                capture_output=True,
                text=True,
                env=env,
                timeout=self.command_timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return subprocess.CompletedProcess(command, 127, "", str(exc))

    @staticmethod
    def _egress_gateway_enabled() -> bool:
        configured = os.getenv("EGRESS_GATEWAY_ENABLED", "1").strip().lower()
        return configured not in {"0", "false", "no", "off"}

    def _services_for_mode(self, egress_mode: str) -> tuple[str, ...]:
        services = ["mediamtx", "continuity"]
        if egress_mode != "RELAY_ONLY" and self._egress_gateway_enabled():
            services.append("egress-gateway")
        return tuple(services)

    def _preflight(self) -> SupervisionResult:
        if not self.compose_file.is_file():
            return SupervisionResult(
                False, f"compose file is missing: {self.compose_file}"
            )
        version = self._compose("version")
        if version.returncode != 0:
            return SupervisionResult(
                False,
                version.stderr.strip() or "docker compose is unavailable",
            )
        return SupervisionResult(True, "compose preflight passed")

    @staticmethod
    def _parse_ps(output: str) -> list[dict[str, object]]:
        payload = output.strip()
        if not payload:
            return []
        if payload.startswith("["):
            parsed = json.loads(payload)
            if not isinstance(parsed, list) or any(
                not isinstance(item, dict) for item in parsed
            ):
                raise ValueError("compose ps JSON must be a list of objects")
            return parsed
        rows = []
        for line in payload.splitlines():
            item = json.loads(line)
            if not isinstance(item, dict):
                raise ValueError("compose ps row must be an object")
            rows.append(item)
        return rows

    def _health_from_ps(
        self, result: subprocess.CompletedProcess[str]
    ) -> dict[str, object]:
        if result.returncode != 0:
            return {"media_stack": "unknown", "compose_ok": False}
        try:
            rows = self._parse_ps(result.stdout)
        except (json.JSONDecodeError, ValueError, TypeError):
            return {"media_stack": "unknown", "compose_ok": False}
        by_service = {
            str(row.get("Service", "")): row
            for row in rows
            if row.get("Service")
        }
        if any(service not in by_service for service in self.required_services):
            return {"media_stack": "stopped", "compose_ok": True}
        for service in self.required_services:
            row = by_service[service]
            state = str(row.get("State", "")).lower()
            health = str(row.get("Health", "")).lower()
            if state != "running":
                return {"media_stack": "stopped", "compose_ok": True}
            if health not in {"", "healthy"}:
                return {"media_stack": "starting", "compose_ok": True}
        return {"media_stack": "running", "compose_ok": True}

    def _read_health(self) -> dict[str, object]:
        ps = self._compose(
            "ps", "--all", "--format", "json", *self.required_services
        )
        return self._health_from_ps(ps)

    def start(
        self,
        session_id: str,
        *,
        egress_mode: str = "DIRECT_PUSH",
    ) -> SupervisionResult:
        preflight = self._preflight()
        if not preflight.ok:
            return preflight
        self.required_services = self._services_for_mode(egress_mode)
        if "egress-gateway" not in self.required_services:
            disabled = self._compose("stop", "egress-gateway")
            if disabled.returncode != 0:
                return SupervisionResult(
                    False,
                    disabled.stderr.strip() or "cannot stop disabled egress gateway",
                )
        # Deployment creates the containers with the full production overlays.
        # The agent only starts existing media containers so it cannot silently
        # recreate them without public/TLS configuration.
        result = self._compose("start", *self.required_services)
        if result.returncode != 0:
            return SupervisionResult(
                False, result.stderr.strip() or "compose start failed"
            )
        deadline = time.monotonic() + self.startup_timeout_seconds
        while True:
            health = self._read_health()
            if health.get("media_stack") == "running":
                return SupervisionResult(True, "required media services are running")
            if time.monotonic() >= deadline:
                return SupervisionResult(
                    False,
                    f"media services did not become ready: {health.get('media_stack')}",
                )
            time.sleep(self.poll_interval_seconds)

    def stop(self, session_id: str) -> SupervisionResult:
        services = ("egress-gateway", "continuity", "mediamtx")
        last_detail = "media services stop failed"
        for attempt in range(3):
            stopped = self._compose("stop", "-t", "10", *services)
            if stopped.returncode == 0:
                return SupervisionResult(True, stopped.stderr.strip() or "media services stopped")
            last_detail = stopped.stderr.strip() or last_detail
            if attempt < 2:
                time.sleep(min(0.25 * (attempt + 1), 0.5))
        return SupervisionResult(False, last_detail)

    def health(self) -> dict[str, object]:
        if not self.compose_file.is_file():
            return {"media_stack": "unknown", "compose_ok": False}
        return self._read_health()


class FakeSupervisor(MediaSupervisor):
    """In-memory supervisor used by unit tests and local dry-runs."""

    def __init__(self) -> None:
        self.started_sessions: list[str] = []
        self.started_egress_modes: list[str] = []
        self.stopped_sessions: list[str] = []
        self.running = False

    def start(
        self,
        session_id: str,
        *,
        egress_mode: str = "DIRECT_PUSH",
    ) -> SupervisionResult:
        self.started_sessions.append(session_id)
        self.started_egress_modes.append(egress_mode)
        self.running = True
        return SupervisionResult(True, "fake media stack started")

    def stop(self, session_id: str) -> SupervisionResult:
        self.stopped_sessions.append(session_id)
        self.running = False
        return SupervisionResult(True, "fake media stack stopped")

    def health(self) -> dict[str, object]:
        return {
            "media_stack": "running" if self.running else "stopped",
            "compose_ok": True,
        }
