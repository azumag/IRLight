"""Media process supervision for the IRLight Node Agent.

The Node Agent never passes secrets through process arguments. The Compose
supervisor hands the egress secret to the continuity container via a tmpfs
file mounted as ``EGRESS_URL_FILE``.
"""

from __future__ import annotations

import abc
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
    ) -> None:
        self.compose_file = Path(
            compose_file or os.getenv("NODE_COMPOSE_FILE", "docker-compose.node.yml")
        )
        self.project_name = os.getenv("NODE_COMPOSE_PROJECT", project_name)

    def _compose(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["COMPOSE_PROJECT_NAME"] = self.project_name
        return subprocess.run(
            ["docker", "compose", "-f", str(self.compose_file), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
            check=False,
        )

    @staticmethod
    def _egress_gateway_enabled() -> bool:
        configured = os.getenv("EGRESS_GATEWAY_ENABLED", "1").strip().lower()
        return configured not in {"0", "false", "no", "off"}

    def start(
        self,
        session_id: str,
        *,
        egress_mode: str = "DIRECT_PUSH",
    ) -> SupervisionResult:
        args = ["up", "-d"]
        if egress_mode == "RELAY_ONLY" or not self._egress_gateway_enabled():
            args.extend(["--scale", "egress-gateway=0"])
        result = self._compose(*args)
        if result.returncode != 0:
            return SupervisionResult(False, result.stderr.strip() or "compose up failed")
        time.sleep(3)
        ps = self._compose("ps", "--format", "json")
        return SupervisionResult(
            ps.returncode == 0, ps.stdout.strip() or "media stack started"
        )

    def stop(self, session_id: str) -> SupervisionResult:
        result = self._compose("down", "--remove-orphans")
        return SupervisionResult(result.returncode == 0, result.stderr.strip() or "stopped")

    def health(self) -> dict[str, object]:
        ps = self._compose("ps", "--format", "json")
        healthy = ps.returncode == 0 and "Running" in ps.stdout
        return {
            "media_stack": "running" if healthy else "stopped",
            "compose_ok": ps.returncode == 0,
        }


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
