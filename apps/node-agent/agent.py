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
import ipaddress
import math
import os
import secrets
import signal
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Callable

from egress_status import read_egress_status
from ingest_policy import IngestPolicyInspector
from ingest_quality import IngestQualitySampler
from relay_client import RelayClientObserver
from supervisor import ComposeSupervisor, FakeSupervisor, MediaSupervisor


def _float_env(name: str, default: float, minimum: float, maximum: float | None = None) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        value = default
    if not math.isfinite(value):
        value = default
    value = max(minimum, value)
    if maximum is not None:
        value = min(value, maximum)
    return value


DEFAULT_HEARTBEAT_INTERVAL = _float_env("NODE_HEARTBEAT_INTERVAL", 10.0, 0.05, maximum=60.0)
DEFAULT_BOOTSTRAP_TIMEOUT = _float_env(
    "NODE_BOOTSTRAP_TIMEOUT_SECONDS", 120.0, 0.0, maximum=600.0
)
DEFAULT_BOOTSTRAP_RETRY = _float_env("NODE_BOOTSTRAP_RETRY_SECONDS", 2.0, 0.05, maximum=30.0)


def _atomic_write_secret(path: Path, value: str) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _atomic_write_json(path: Path, value: dict[str, object]) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


class ControlPlaneHTTPError(RuntimeError):
    """A definitive HTTP response that must not be blindly retried."""

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        super().__init__(f"control plane HTTP {status_code}: {body[:300]}")


class ControlPlaneUnavailable(RuntimeError):
    """A transport failure that is safe to retry before bootstrap completes."""


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
        try:
            body = exc.read().decode("utf-8", errors="replace")
        finally:
            exc.close()
        raise ControlPlaneHTTPError(exc.code, body) from exc
    except urllib.error.URLError as exc:
        raise ControlPlaneUnavailable(
            f"control plane unavailable: {exc.reason}"
        ) from exc
    except (TimeoutError, OSError) as exc:
        raise ControlPlaneUnavailable(f"control plane unavailable: {exc}") from exc


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
        bootstrap_timeout_seconds: float = DEFAULT_BOOTSTRAP_TIMEOUT,
        bootstrap_retry_seconds: float = DEFAULT_BOOTSTRAP_RETRY,
        ingest_inspector: IngestPolicyInspector | None = None,
        ingest_quality_sampler: IngestQualitySampler | None = None,
        egress_status_file: Path | None = None,
        relay_client_observer: RelayClientObserver | None = None,
        pre_media_start: Callable[[str], None] | None = None,
        post_media_stop: Callable[[], None] | None = None,
        control_state_path: Path | None = None,
    ) -> None:
        self.control_base_url = control_base_url.rstrip("/")
        self.bootstrap_token = bootstrap_token
        self.provider_server_id = provider_server_id
        self.boot_id = boot_id
        self.agent_version = agent_version
        self.secret_dir = secret_dir
        self.supervisor = supervisor
        self.heartbeat_interval = heartbeat_interval
        self.bootstrap_timeout_seconds = max(0.0, bootstrap_timeout_seconds)
        self.bootstrap_retry_seconds = max(0.05, bootstrap_retry_seconds)
        self.egress_mode = "DIRECT_PUSH"
        self.ingest_inspector = ingest_inspector
        self.ingest_quality_sampler = ingest_quality_sampler
        self.egress_status_file = egress_status_file
        self.relay_client_observer = relay_client_observer
        self.pre_media_start = pre_media_start
        self.post_media_stop = post_media_stop
        self.control_state_path = control_state_path
        self.node_id: str | None = None
        self.session_id: str | None = None
        self.absolute_deadline: float | None = None
        # The Agent owns this raw token. The Control Plane stores only its
        # digest, making identical bootstrap retries safe after response loss.
        self.bootstrap_request_id = str(uuid.uuid4())
        self.node_access_token: str | None = secrets.token_urlsafe(32)
        self._stop_requested = False
        self._received_signal = False
        self._shutdown_event = threading.Event()
        self._media_stop_lock = threading.Lock()
        self._media_stop_attempted = False
        self._media_started = False
        self._media_start_attempted = False
        self._media_start_finished = False
        self._media_stop_requested = False
        self._media_stop_result = None
        self._media_stop_error: Exception | None = None

    # -- bootstrap ---------------------------------------------------------

    def bootstrap(self) -> dict[str, object]:
        payload = {
            "provider_server_id": self.provider_server_id,
            "boot_id": self.boot_id,
            "agent_version": self.agent_version,
            "bootstrap_request_id": self.bootstrap_request_id,
            "node_access_token": self.node_access_token,
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
        returned_access_token = str(response.get("node_access_token", "")) or None
        if returned_access_token and not secrets.compare_digest(
            returned_access_token, self.node_access_token or ""
        ):
            raise RuntimeError("bootstrap response returned a mismatched node access token")
        egress_mode = str(response.get("egress_mode", "DIRECT_PUSH"))
        if egress_mode not in {"DIRECT_PUSH", "RELAY_ONLY"}:
            raise RuntimeError("bootstrap response has unsupported egress_mode")
        self.egress_mode = egress_mode
        if not self.node_id or not self.session_id or not self.node_access_token:
            raise RuntimeError(
                "bootstrap response missing node_id, session_id or node access token"
            )
        return response

    def bootstrap_with_retry(self) -> dict[str, object]:
        """Retry transport-only startup races without retrying HTTP denials."""
        deadline = time.monotonic() + self.bootstrap_timeout_seconds
        while True:
            try:
                return self.bootstrap()
            except ControlPlaneUnavailable as exc:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                delay = min(self.bootstrap_retry_seconds, remaining)
                print(
                    f"[agent] bootstrap unavailable; retrying in {delay:.1f}s: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(delay)

    def write_secret(self, response: dict[str, object]) -> Path | None:
        """Persist the delivered secret to tmpfs, created as 0600 immediately.

        Relay-only sessions intentionally have no outbound credential. Clear
        any stale files so an accidental gateway start cannot reuse them.
        """
        egress_mode = str(response.get("egress_mode", self.egress_mode))
        if egress_mode == "RELAY_ONLY":
            for name in ("egress_url", "egress_verified_peer_ip"):
                try:
                    (self.secret_dir / name).unlink()
                except FileNotFoundError:
                    pass
            return None

        self.secret_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.secret_dir.chmod(0o700)
        except OSError:
            pass
        egress_url = str(response.get("egress_url", ""))
        if not egress_url:
            raise RuntimeError("bootstrap response missing egress_url")
        peer_ip = str(response.get("egress_verified_peer_ip", "")).strip()
        try:
            parsed_peer = ipaddress.ip_address(peer_ip)
        except ValueError as exc:
            raise RuntimeError("bootstrap response missing verified destination peer IP") from exc
        if parsed_peer.is_unspecified or parsed_peer.is_loopback or parsed_peer.is_multicast or parsed_peer.is_link_local:
            raise RuntimeError("bootstrap response has unusable destination peer IP")
        secret_path = self.secret_dir / "egress_url"
        peer_path = self.secret_dir / "egress_verified_peer_ip"
        try:
            _atomic_write_secret(secret_path, egress_url)
            _atomic_write_secret(peer_path, peer_ip)
        except Exception:
            for path in (secret_path, peer_path):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            raise
        return secret_path

    def seed_control_state(self, response: dict[str, object]) -> None:
        """Seed a fresh node's audio authority before Continuity starts.

        The Control Plane state volume is not present on a production media
        node.  A bootstrap response therefore carries the initial command;
        existing state is never overwritten, preserving operator changes over
        agent restarts.
        """
        if self.control_state_path is None or self.control_state_path.exists():
            return
        mode = str(response.get("audio_mode", "LIVE"))
        if mode not in {"LIVE", "MUTED"}:
            raise RuntimeError("bootstrap response has unsupported audio mode")
        payload = {
            "audio_mode": mode,
            "version": int(response.get("audio_version", 0) or 0),
            "command_id": response.get("audio_command_id"),
            "idempotency_key": response.get("audio_idempotency_key"),
            "updated_at": float(response.get("audio_updated_at", time.time())),
        }
        self.control_state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _atomic_write_json(self.control_state_path, payload)

    @staticmethod
    def remove_secret(secret_path: Path | None) -> None:
        if secret_path is None:
            return
        for path in (secret_path, secret_path.with_name("egress_verified_peer_ip")):
            try:
                path.unlink()
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
        if self.egress_status_file is None or self.egress_mode == "RELAY_ONLY":
            return None
        return read_egress_status(self.egress_status_file)

    def _relay_client_observation(self) -> dict[str, object] | None:
        if self.egress_mode != "RELAY_ONLY" or self.relay_client_observer is None:
            return None
        try:
            return self.relay_client_observer.observe()
        except RuntimeError as exc:
            return {
                "status": "UNKNOWN",
                "connected": False,
                "reader_count": 0,
                "reason_code": str(exc)[:100],
                "observed_at": time.time(),
            }

    def heartbeat(self) -> dict[str, object]:
        if self.node_id is None:
            raise RuntimeError("heartbeat before bootstrap")
        health = self.supervisor.health()
        ingest = self._ingest_observation()
        egress = self._egress_observation()
        relay_client = self._relay_client_observation()
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
        if relay_client is not None:
            payload["relay_client"] = relay_client
        return http_json(
            f"{self.control_base_url}/internal/nodes/{self.node_id}/heartbeat",
            method="POST",
            payload=payload,
            token=self.node_access_token,
        )

    # -- stop handling -----------------------------------------------------

    def handle_signal(self, signum: int, _frame: object) -> None:
        self._received_signal = True
        self._shutdown_event.set()

    def _stop_media_once(self, session_id: str) -> None:
        """Stop the media stack exactly once, including watchdog/finally races.

        A deadline can fire while ``start`` is still blocked.  In that case
        record the request and perform the stop immediately after start returns
        successfully; never allow a late start to leave media running.
        """
        with self._media_stop_lock:
            self._media_stop_requested = True
            if not self._media_started and self._media_start_attempted and not self._media_start_finished:
                return
            if self._media_stop_attempted:
                return
            self._media_stop_attempted = True
            try:
                self._media_stop_result = self.supervisor.stop(session_id)
                if self._media_stop_result.ok:
                    self._media_started = False
                    self._media_stop_error = None
            except Exception as exc:
                self._media_stop_error = exc
                # A failed stop is retryable from the finalizer (and from a
                # subsequent watchdog invocation); never claim success merely
                # because an attempt was made.
                self._media_stop_attempted = False
                print(
                    f"[agent] supervisor stop failed: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                return
            print(
                f"[agent] media stack stopped: {self._media_stop_result.detail}",
                flush=True,
            )

    def _deadline_watchdog(self, session_id: str) -> None:
        """Enforce signal/deadline shutdown independently of heartbeat I/O."""
        while not self._shutdown_event.is_set():
            if self.absolute_deadline is None:
                self._shutdown_event.wait(0.1)
                continue
            remaining = self.absolute_deadline - time.time()
            if remaining <= 0:
                print(
                    "[agent] absolute deadline reached; stopping media",
                    flush=True,
                )
                self._stop_requested = True
                self._shutdown_event.set()
                break
            self._shutdown_event.wait(min(0.1, remaining))
        self._stop_media_once(session_id)

    def run(self) -> int:
        try:
            signal.signal(signal.SIGTERM, self.handle_signal)
            signal.signal(signal.SIGINT, self.handle_signal)
        except ValueError:
            # Not running in the main thread (tests); rely on STOP via
            # heartbeat responses instead.
            pass

        session_id = "unknown"
        secret_path: Path | None = None
        media_auth_started = False
        watchdog: threading.Thread | None = None
        exit_code = 0
        try:
            print(f"[agent] bootstrap start provider={self.provider_server_id}", flush=True)
            response = self.bootstrap_with_retry()
            session_id = self.session_id or "unknown"
            if self.absolute_deadline is not None and self.absolute_deadline <= time.time():
                print(
                    "[agent] refusing media start after absolute deadline",
                    file=sys.stderr,
                    flush=True,
                )
                self._stop_requested = True
                self._shutdown_event.set()
                exit_code = 1
                return exit_code
            secret_path = self.write_secret(response)
            self.seed_control_state(response)
            if secret_path is None:
                print(
                    f"[agent] bootstrap ok node={self.node_id} session={self.session_id} "
                    "egress=RELAY_ONLY",
                    flush=True,
                )
            else:
                print(
                    f"[agent] bootstrap ok node={self.node_id} session={self.session_id} "
                    f"secret={secret_path}",
                    flush=True,
                )

            watchdog = threading.Thread(
                target=self._deadline_watchdog,
                args=(session_id,),
                name="media-deadline-watchdog",
                daemon=True,
            )
            watchdog.start()

            if not self._shutdown_event.is_set():
                if self.pre_media_start is not None:
                    self.pre_media_start(self.node_access_token or "")
                    media_auth_started = True
            if not self._shutdown_event.is_set():
                self._media_start_attempted = True
                # Treat the stack as potentially running for the entire
                # supervisor call: compose may have started containers before
                # its health wait returns, and a watchdog must be able to stop
                # that partially-started stack concurrently.
                with self._media_stop_lock:
                    self._media_started = True
                try:
                    result = self.supervisor.start(
                        session_id,
                        egress_mode=self.egress_mode,
                    )
                except Exception as exc:
                    self._media_start_finished = True
                    print(
                        f"[agent] supervisor start failed: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                    exit_code = 1
                else:
                    if not result.ok:
                        self._media_start_finished = True
                        print(
                            f"[agent] supervisor start failed: {result.detail}",
                            file=sys.stderr,
                            flush=True,
                        )
                        exit_code = 1
                    else:
                        print(f"[agent] media stack started: {result.detail}", flush=True)
                        with self._media_stop_lock:
                            self._media_started = True
                            self._media_start_finished = True
                            stop_after_start = self._media_stop_requested or self._shutdown_event.is_set()
                        if stop_after_start:
                            self._stop_media_once(session_id)
                        while not self._shutdown_event.is_set():
                            try:
                                heartbeat_response = self.heartbeat()
                                desired = str(
                                    heartbeat_response.get("desired_state", "RUNNING")
                                )
                                if desired == "STOPPED":
                                    self._stop_requested = True
                                    self._shutdown_event.set()
                                    break
                            except ControlPlaneHTTPError as exc:
                                print(
                                    f"[agent] heartbeat denied: {exc}",
                                    file=sys.stderr,
                                    flush=True,
                                )
                                if 400 <= exc.status_code < 500:
                                    self._stop_requested = True
                                    self._shutdown_event.set()
                                    break
                            except RuntimeError as exc:
                                print(
                                    f"[agent] heartbeat failed: {exc}",
                                    file=sys.stderr,
                                    flush=True,
                                )

                            self._shutdown_event.wait(self.heartbeat_interval)
        finally:
            self._shutdown_event.set()
            self._stop_media_once(session_id)
            if watchdog is not None:
                watchdog.join(timeout=1.0)
            # If stop raced with a supervisor that was still starting, retry
            # once after the start path has fully unwound.
            self._stop_media_once(session_id)
            if self._media_stop_error is not None:
                exit_code = 1
            elif self._media_stop_result is not None and not self._media_stop_result.ok:
                exit_code = 1
            if self.post_media_stop is not None:
                try:
                    self.post_media_stop()
                except Exception as exc:
                    print(
                        f"[agent] media auth cleanup failed: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                    exit_code = 1
            elif media_auth_started:
                # Defensive only: the lifecycle must always provide cleanup.
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


def build_relay_client_observer() -> RelayClientObserver | None:
    if os.getenv("NODE_RELAY_CLIENT_OBSERVER_ENABLED", "1") == "0":
        return None
    return RelayClientObserver()


def main(
    *,
    pre_media_start: Callable[[str], None] | None = None,
    post_media_stop: Callable[[], None] | None = None,
) -> int:
    secret_dir = Path(
        _env("NODE_EGRESS_SECRET_DIR", required=False)
        or _env("NODE_SECRET_DIR", required=False)
        or "/run/irlight/egress-secrets"
    )
    egress_status_path = _env("NODE_EGRESS_STATUS_FILE", required=False)
    control_state_path = _env("NODE_CONTROL_STATE_FILE", required=False) or "/state/control.json"
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
        relay_client_observer=build_relay_client_observer(),
        pre_media_start=pre_media_start,
        post_media_stop=post_media_stop,
        control_state_path=Path(control_state_path),
    )
    return agent.run()


if __name__ == "__main__":
    raise SystemExit(main())
