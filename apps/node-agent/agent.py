"""IRLight Node Agent (Phase B spike).

Responsibilities:

- exchange the one-time bootstrap token with the Control Plane
- write the delivered egress secret to a tmpfs file with 0600 permissions
- start the media stack through the configured supervisor
- inspect and enforce the ingest format/bitrate policy through MediaMTX
- sample FPS/GOP/timestamp health without exposing the RTSP path publicly
- report safe Egress Gateway status without copying destination credentials
- send heartbeats and honour STOP / absolute deadline

Secrets are never placed in process arguments or container environment
variables. The production compose file mounts the tmpfs secret read-only into
the dedicated Egress Gateway.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from egress_status import read_egress_status
from ingest_policy import IngestPolicyInspector
from ingest_quality import IngestQualitySampler
from supervisor import ComposeSupervisor, FakeSupervisor, MediaSupervisor


DEFAULT_HEARTBEAT_INTERVAL = float(os.getenv("NODE_HEARTBEAT_INTERVAL", "10"))


def _env(name: str, required: bool = True) -> str:
    value = os.getenv(name, "")
    if required and not value:
        raise RuntimeError(f"missing required env var: {name}")
    return value


def _secret_from_file_or_env(name: str) -> str:
    """Read a secret from a file (``NAME_FILE``) or a plain env var.

    Prefer files: ``docker inspect`` cannot recover the value from a file.
    """
    file_env = f"{name}_FILE"
    file_path = os.getenv(file_env)
    if file_path:
        try:
            value = Path(file_path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(f"cannot read {file_env}={file_path}: {exc}") from exc
        if value:
            return value
    return _env(name)


def http_json(
    url: str,
    *,
    method: str,
    token: str | None = None,
    payload: dict[str, object] | None = None,
    timeout: float = 15.0,
) -> dict[str, object]:
    headers = {"Accept": "application/json"}
    data = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            if not raw:
                return {}
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError("control plane returned invalid JSON") from exc
            return value if isinstance(value, dict) else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"control plane HTTP {exc.code}: {body[:300]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"control plane unavailable: {exc.reason}") from exc
    except (TimeoutError, OSError) as exc:
        raise RuntimeError(f"control plane unavailable: {exc}") from exc


class NodeAgent:
    def __init__(
        self,
        *,
        control_base_url: str,
        bootstrap_token: str,
        provider_server_id: str,
        boot_id: str,
        agent_version: str,
        secret_dir: Path,
        supervisor: MediaSupervisor,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
        ingest_inspector: IngestPolicyInspector | None = None,
        ingest_quality_sampler: IngestQualitySampler | None = None,
        egress_status_file: Path | None = None,
    ) -> None:
        self.control_base_url = control_base_url.rstrip("/")
        self.bootstrap_token = bootstrap_token
        self.provider_server_id = provider_server_id
        self.boot_id = boot_id
        self.agent_version = agent_version
        self.secret_dir = secret_dir
        self.supervisor = supervisor
        self.heartbeat_interval = heartbeat_interval
        self.ingest_inspector = ingest_inspector
        self.ingest_quality_sampler = ingest_quality_sampler
        self.egress_status_file = egress_status_file
        self.node_id: str | None = None
        self.session_id: str | None = None
        self.absolute_deadline: float | None = None
        self._stop_requested = False
        self._received_signal = False

    # -- bootstrap ---------------------------------------------------------

    def bootstrap(self) -> dict[str, object]:
        payload = {
            "provider_server_id": self.provider_server_id,
            "boot_id": self.boot_id,
            "agent_version": self.agent_version,
        }
        response = http_json(
            f"{self.control_base_url}/internal/nodes/bootstrap",
            method="POST",
            token=self.bootstrap_token,
            payload=payload,
        )
        self.node_id = str(response.get("node_id", "")) or None
        self.session_id = str(response.get("session_id", "")) or None
        self.absolute_deadline = _as_float(response.get("absolute_deadline"))
        if not self.node_id or not self.session_id:
            raise RuntimeError("bootstrap response missing node_id or session_id")
        return response

    def write_secret(self, response: dict[str, object]) -> Path:
        """Persist the delivered secret to tmpfs, created as 0600 immediately."""
        self.secret_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.secret_dir.chmod(0o700)
        except OSError:
            pass
        egress_url = str(response.get("egress_url", ""))
        if not egress_url:
            raise RuntimeError("bootstrap response missing egress_url")
        secret_path = self.secret_dir / "egress_url"
        fd = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(egress_url + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            try:
                secret_path.unlink()
            except FileNotFoundError:
                pass
            raise
        secret_path.chmod(0o600)
        return secret_path

    @staticmethod
    def remove_secret(secret_path: Path) -> None:
        try:
            secret_path.unlink()
        except FileNotFoundError:
            pass

    # -- heartbeat ---------------------------------------------------------

    def _ingest_observation(self) -> dict[str, object] | None:
        if self.ingest_inspector is None:
            return None
        try:
            observation = self.ingest_inspector.observe_and_enforce()
        except RuntimeError as exc:
            print(f"[agent] ingest inspection failed: {exc}", file=sys.stderr, flush=True)
            return {
                "status": "UNKNOWN",
                "path": os.getenv("NODE_INGEST_PATH", "live/input"),
                "online": False,
                "source_type": None,
                "source_id": None,
                "bitrate_bps": None,
                "tracks": [],
                "reasons": ["MEDIAMTX_API_UNAVAILABLE"],
                "warnings": [],
                "enforced": False,
                "observed_at": time.time(),
            }

        if self.ingest_quality_sampler is None:
            return observation
        try:
            return self.ingest_quality_sampler.augment(observation)
        except Exception as exc:  # quality must never break the heartbeat loop
            print(f"[agent] ingest quality sample failed: {exc}", file=sys.stderr, flush=True)
            degraded = dict(observation)
            if degraded.get("online") and degraded.get("status") != "REJECTED":
                warnings = list(degraded.get("warnings", []))
                warnings.append("QUALITY_SAMPLER_UNAVAILABLE")
                degraded["warnings"] = list(dict.fromkeys(warnings))
                if degraded.get("status") == "ACCEPTED":
                    degraded["status"] = "WARNING"
            return degraded

    def _egress_observation(self) -> dict[str, object] | None:
        if self.egress_status_file is None:
            return None
        return read_egress_status(self.egress_status_file)

    def heartbeat(self) -> dict[str, object]:
        if self.node_id is None:
            raise RuntimeError("heartbeat before bootstrap")
        health = self.supervisor.health()
        ingest = self._ingest_observation()
        egress = self._egress_observation()
        remaining = None
        if self.absolute_deadline is not None:
            remaining = max(0.0, self.absolute_deadline - time.time())
        active_publisher = bool(health.get("active_publisher", False))
        if ingest is not None:
            active_publisher = bool(ingest.get("online", False))
        egress_connected = bool(health.get("egress_connected", False))
        if egress is not None:
            egress_connected = bool(egress.get("connected", False))
        payload: dict[str, object] = {
            "status": "READY" if health.get("media_stack") == "running" else "STOPPING",
            "media_health": str(health.get("media_stack", "unknown")),
            "active_publisher": active_publisher,
            "egress_connected": egress_connected,
            "software_version": self.agent_version,
            "deadline_remaining_seconds": remaining,
        }
        if ingest is not None:
            payload["ingest"] = ingest
        if egress is not None:
            payload["egress"] = egress
        return http_json(
            f"{self.control_base_url}/internal/nodes/{self.node_id}/heartbeat",
            method="POST",
            payload=payload,
        )

    # -- stop handling -----------------------------------------------------

    def handle_signal(self, signum: int, _frame: object) -> None:
        self._received_signal = True

    def run(self) -> int:
        try:
            signal.signal(signal.SIGTERM, self.handle_signal)
            signal.signal(signal.SIGINT, self.handle_signal)
        except ValueError:
            # Not running in the main thread (tests); rely on STOP via
            # heartbeat responses instead.
            pass

        print(f"[agent] bootstrap start provider={self.provider_server_id}", flush=True)
        response = self.bootstrap()
        secret_path = self.write_secret(response)
        print(
            f"[agent] bootstrap ok node={self.node_id} session={self.session_id} "
            f"secret={secret_path}",
            flush=True,
        )

        exit_code = 0
        result = self.supervisor.start(self.session_id or "unknown")
        if not result.ok:
            print(f"[agent] supervisor start failed: {result.detail}", file=sys.stderr, flush=True)
            exit_code = 1
        else:
            print(f"[agent] media stack started: {result.detail}", flush=True)
            while not (self._stop_requested or self._received_signal):
                try:
                    heartbeat_response = self.heartbeat()
                    desired = str(heartbeat_response.get("desired_state", "RUNNING"))
                    if desired == "STOPPED":
                        self._stop_requested = True
                        break
                except RuntimeError as exc:
                    print(f"[agent] heartbeat failed: {exc}", file=sys.stderr, flush=True)

                if (
                    self.absolute_deadline is not None
                    and time.time() >= self.absolute_deadline
                ):
                    print("[agent] absolute deadline reached; stopping media", flush=True)
                    break
                time.sleep(self.heartbeat_interval)

        stop_result = self.supervisor.stop(self.session_id or "unknown")
        print(f"[agent] media stack stopped: {stop_result.detail}", flush=True)
        if not stop_result.ok:
            exit_code = 1
        self.remove_secret(secret_path)
        return exit_code


def _as_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def build_supervisor() -> MediaSupervisor:
    mode = os.getenv("NODE_SUPERVISOR", "compose")
    if mode == "fake":
        return FakeSupervisor()
    if mode == "compose":
        return ComposeSupervisor()
    raise RuntimeError(f"unsupported NODE_SUPERVISOR: {mode}")


def build_ingest_inspector() -> IngestPolicyInspector | None:
    if os.getenv("NODE_INGEST_POLICY_ENABLED", "1") == "0":
        return None
    return IngestPolicyInspector()


def build_ingest_quality_sampler() -> IngestQualitySampler | None:
    if os.getenv("NODE_INGEST_QUALITY_ENABLED", "1") == "0":
        return None
    return IngestQualitySampler()


def main() -> int:
    secret_dir = Path(_env("NODE_SECRET_DIR", required=False) or "/run/irlight/secrets")
    egress_status_path = _env("NODE_EGRESS_STATUS_FILE", required=False)
    agent = NodeAgent(
        control_base_url=_env("NODE_CONTROL_PLANE_URL"),
        bootstrap_token=_secret_from_file_or_env("NODE_BOOTSTRAP_TOKEN"),
        provider_server_id=_env("NODE_PROVIDER_SERVER_ID"),
        boot_id=_env("NODE_BOOT_ID", required=False) or "local-boot",
        agent_version=_env("NODE_AGENT_VERSION", required=False) or "0.5.0-spike",
        secret_dir=secret_dir,
        supervisor=build_supervisor(),
        ingest_inspector=build_ingest_inspector(),
        ingest_quality_sampler=build_ingest_quality_sampler(),
        egress_status_file=Path(egress_status_path) if egress_status_path else None,
    )
    return agent.run()


if __name__ == "__main__":
    raise SystemExit(main())
