"""Internal APIs used by Node Agents (bootstrap / heartbeat / stop).

These endpoints are intentionally separate from the user-facing API. They are
only reachable from inside the trusted network in the production compose file;
they must never be exposed to the public internet.
"""

from __future__ import annotations

import hashlib
import fcntl
import json
import math
import os
import secrets
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from catalog_store import CatalogNotFound, get_destination as store_get_destination
from destination_secret_store import (
    DestinationSecretConfigurationError,
    DestinationSecretError,
    DestinationSecretNotFound,
    default_destination_secret_store,
)
from egress_destination import EgressDestinationError, build_egress_url
from fake_provider_for_api import default_store, provider_mode
from pipeline_health import apply_pipeline_health
from session_store import ACTIVE_STATES
from state_safety import mark_initialized, was_initialized


def _state_dir() -> Path:
    # Tests patch ``node_internal.STATE_DIR`` to a concrete temp dir via
    # ``patch.object``. Respect that patch so integration tests keep working
    # even after we made the module-level constants dynamic.
    current = globals().get("STATE_DIR")
    if isinstance(current, Path):
        # Plain Path patch from a test - use it directly. DynamicPath is not a
        # Path subclass, so this distinguishes a real patch from the proxy.
        return current
    # Fallback to env-based dynamic path for normal operation and for the
    # shared ``DynamicPath`` proxy.
    return Path(os.getenv("NODE_STATE_DIR", os.getenv("STATE_DIR", "/state")))


def _nodes_path() -> Path:
    # Tests patch ``NODES_PATH`` directly; respect that.
    current = globals().get("NODES_PATH")
    if isinstance(current, Path):
        return current
    state = _state_dir()
    # ``state`` may be a DynamicPath proxy; ensure we return a real Path.
    if isinstance(state, _DynamicPath):
        state = state._path()
    return state / "nodes.json"


def _tokens_path() -> Path:
    current = globals().get("TOKENS_PATH")
    if isinstance(current, Path):
        return current
    state = _state_dir()
    if isinstance(state, _DynamicPath):
        state = state._path()
    return state / "bootstrap_tokens.json"


class _DynamicPath:
    """Proxy that always resolves to the current env-based path.

    ``from node_internal import NODES_PATH`` captures this object once; every
    attribute access re-resolves the underlying Path so later
    ``NODE_STATE_DIR`` changes are still observed.
    """

    def __init__(self, getter):
        self._getter = getter

    def _path(self) -> Path:
        return self._getter()

    def __fspath__(self) -> str:
        return os.fspath(self._path())

    def __str__(self) -> str:
        return str(self._path())

    def __repr__(self) -> str:
        return repr(self._path())

    def __truediv__(self, other):
        return self._path() / other

    def __rtruediv__(self, other):
        return other / self._path()

    def __getattr__(self, name: str):
        return getattr(self._path(), name)

    def __eq__(self, other) -> bool:
        if isinstance(other, _DynamicPath):
            return self._path() == other._path()
        if isinstance(other, Path):
            return self._path() == other
        return False

    def __hash__(self) -> int:
        return hash(self._path())


# Legacy module-level constants for callers that imported them once.
# They are dynamic proxies so ``from node_internal import NODES_PATH`` inside
# a test still follows later ``NODE_STATE_DIR`` changes.
STATE_DIR = _DynamicPath(_state_dir)
NODES_PATH = _DynamicPath(_nodes_path)
TOKENS_PATH = _DynamicPath(_tokens_path)


def _refresh_legacy_paths() -> None:
    # Kept for compatibility; dynamic proxies already follow the env.
    pass


def __getattr__(name: str) -> Path:  # PEP 562
    if name == "STATE_DIR":
        return _state_dir()
    if name == "NODES_PATH":
        return _nodes_path()
    if name == "TOKENS_PATH":
        return _tokens_path()
    raise AttributeError(name)


NODE_STATE_THREAD_LOCK = threading.RLock()


class NodeStateError(RuntimeError):
    """Raised when persisted Node authority cannot be read safely."""


@contextmanager
def node_state_lock(*, exclusive: bool):
    """Serialize Node/token transactions across threads and API workers."""
    with NODE_STATE_THREAD_LOCK:
        _refresh_legacy_paths()
        try:
            _state_dir().mkdir(parents=True, exist_ok=True)
            lock_path = _state_dir() / ".node-state.lock"
            with lock_path.open("a+", encoding="utf-8") as handle:
                operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
                fcntl.flock(handle.fileno(), operation)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except NodeStateError:
            raise
        except OSError as exc:
            raise NodeStateError("cannot lock Node state") from exc


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


BoundedReason = Annotated[str, Field(min_length=1, max_length=100)]


class BootstrapRequest(StrictRequest):
    provider_server_id: str = Field(min_length=1, max_length=200)
    boot_id: str = Field(min_length=1, max_length=200)
    agent_version: str = Field(min_length=1, max_length=100)
    bootstrap_request_id: str = Field(min_length=16, max_length=200)
    node_access_token: str = Field(min_length=32, max_length=200)
    public_address: str | None = Field(default=None, max_length=200)
    private_address: str | None = Field(default=None, max_length=200)


class TrackObservation(StrictRequest):
    codec: str = Field(min_length=1, max_length=100)
    width: int | None = Field(default=None, ge=1, le=16_384)
    height: int | None = Field(default=None, ge=1, le=16_384)
    profile: str | int | None = Field(default=None)
    level: str | int | None = Field(default=None)
    sampleRate: int | None = Field(default=None, ge=1, le=768_000)
    channelCount: int | None = Field(default=None, ge=1, le=64)


class QualityObservation(StrictRequest):
    sample_elapsed_seconds: float | None = Field(default=None, ge=0, le=60)
    video_frames: int | None = Field(default=None, ge=0, le=1_000_000)
    audio_frames: int | None = Field(default=None, ge=0, le=10_000_000)
    video_fps: float | None = Field(default=None, ge=0, le=1_000)
    video_timestamp_span_seconds: float | None = Field(default=None, ge=0, le=60)
    audio_timestamp_span_seconds: float | None = Field(default=None, ge=0, le=60)
    keyframes: int | None = Field(default=None, ge=0, le=1_000_000)
    max_gop_seconds: float | None = Field(default=None, ge=0, le=60)
    video_timestamp_regressions: int | None = Field(default=None, ge=0, le=1_000_000)
    audio_timestamp_regressions: int | None = Field(default=None, ge=0, le=1_000_000)
    error: str | None = Field(default=None, max_length=200)


class IngestObservationRequest(StrictRequest):
    status: str = Field(
        pattern="^(OFFLINE|UNKNOWN|PENDING|ACCEPTED|WARNING|DEGRADED|REJECTED)$"
    )
    path: str = Field(min_length=1, max_length=200)
    online: bool = False
    source_type: str | None = Field(default=None, max_length=50)
    source_id: str | None = Field(default=None, max_length=200)
    bitrate_bps: float | None = Field(default=None, ge=0)
    max_bitrate_bps: int | None = Field(default=None, ge=0)
    tracks: list[TrackObservation] = Field(default_factory=list, max_length=8)
    reasons: list[BoundedReason] = Field(default_factory=list, max_length=16)
    warnings: list[BoundedReason] = Field(default_factory=list, max_length=16)
    quality: QualityObservation | None = None
    enforced: bool = False
    enforcement_error: str | None = Field(default=None, max_length=200)
    observed_at: float


class EgressObservationRequest(StrictRequest):
    status: str = Field(
        pattern="^(UNKNOWN|STARTING|CONNECTED|RECONNECTING|AUTH_FAILED|FAILED|STOPPED)$"
    )
    connected: bool = False
    attempt: int = Field(default=0, ge=0)
    reason_code: str | None = Field(default=None, max_length=100)
    rendered_buffers: int = Field(default=0, ge=0)
    next_retry_at: float | None = None
    destination_scheme: str | None = Field(default=None, max_length=20)
    destination_host: str | None = Field(default=None, max_length=253)
    observed_at: float


class RelayClientObservationRequest(StrictRequest):
    status: str = Field(pattern="^(UNKNOWN|CONNECTED|DISCONNECTED)$")
    connected: bool = False
    reader_count: int = Field(default=0, ge=0)
    reason_code: str | None = Field(default=None, max_length=100)
    observed_at: float


class HeartbeatRequest(StrictRequest):
    status: str = Field(
        default="READY", pattern="^(BOOTSTRAPPING|READY|STOPPING|STOPPED|FAILED)$"
    )
    media_health: str = Field(default="unknown", max_length=200)
    active_publisher: bool = False
    egress_connected: bool = False
    cpu_percent: float | None = Field(default=None, ge=0, le=1000)
    memory_mb: float | None = Field(default=None, ge=0)
    software_version: str | None = Field(default=None, max_length=100)
    deadline_remaining_seconds: float | None = Field(default=None, ge=0)
    ingest: IngestObservationRequest | None = None
    egress: EgressObservationRequest | None = None
    relay_client: RelayClientObservationRequest | None = None


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
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


def read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError) as exc:
        raise NodeStateError(f"cannot read Node state {path}") from exc
    if not isinstance(value, dict):
        raise NodeStateError(f"invalid Node state payload in {path}")
    return value


def _default_nodes() -> dict[str, Any]:
    return {"nodes": {}, "next_node_seq": 1, "tokens": {}}


def _default_tokens() -> dict[str, Any]:
    return {"tokens": {}}


def ensure_state() -> None:
    with node_state_lock(exclusive=True):
        _refresh_legacy_paths()
        _state_dir().mkdir(parents=True, exist_ok=True)
        if not _nodes_path().exists():
            if (
                was_initialized(_nodes_path())
                or _tokens_path().exists()
                or was_initialized(_tokens_path())
            ):
                raise NodeStateError(
                    "Node authority disappeared after initialization"
                )
            _write_authority(_default_nodes())
            return

        authority = _validate_nodes(read_json(_nodes_path(), _default_nodes()))
        if not isinstance(authority.get("tokens"), dict):
            # One-time migration from the former split token ledger. Keeping
            # the old file is intentional: rollback must not silently lose it.
            if _tokens_path().exists():
                legacy = _validate_tokens(read_json(_tokens_path(), _default_tokens()))
                authority["tokens"] = dict(legacy["tokens"])
                _write_authority(authority)
            elif authority.get("nodes"):
                raise NodeStateError(
                    "bootstrap token ledger is missing for existing Nodes"
                )
            else:
                authority["tokens"] = {}
                _write_authority(authority)
        else:
            _validate_tokens(authority)
            mark_initialized(_nodes_path())


def _read_authority() -> dict[str, Any]:
    _refresh_legacy_paths()
    if not _nodes_path().exists():
        detail = " disappeared after initialization" if was_initialized(_nodes_path()) else " is missing"
        raise NodeStateError(f"Node authority {_nodes_path()}{detail}")
    authority = _validate_nodes(read_json(_nodes_path(), _default_nodes()))
    _validate_tokens(authority)
    # The rollback ledger is also a write-ahead consumption fuse. If a process
    # dies after committing that fuse but before replacing nodes.json, merge
    # every consumed record into the in-memory authority so the current build
    # also rejects reuse. A later successful authority write persists it.
    if _tokens_path().exists():
        legacy = _validate_tokens(read_json(_tokens_path(), _default_tokens()))
        for digest, record in legacy["tokens"].items():
            if not isinstance(digest, str) or not isinstance(record, dict):
                raise NodeStateError("bootstrap token fuse has an invalid record")
            if not record.get("consumed"):
                continue
            canonical = authority["tokens"].get(digest)
            if not isinstance(canonical, dict) or not canonical.get("consumed"):
                authority["tokens"][digest] = dict(record)
    elif was_initialized(_tokens_path()):
        # A lost write-ahead fuse could hide consumption that never reached the
        # canonical file. Its absence is therefore not evidence of an empty
        # ledger, even when nodes.json itself remains readable.
        raise NodeStateError("bootstrap token fuse disappeared after initialization")
    return authority


def _write_authority(authority: dict[str, Any]) -> None:
    _refresh_legacy_paths()
    _validate_nodes(authority)
    _validate_tokens(authority)
    atomic_write_json(_nodes_path(), authority)
    mark_initialized(_nodes_path())


def _write_legacy_token_fuse(
    digest: str, *, node_id: str, session_id: str
) -> None:
    """Keep rollback to the former split-ledger build fail-closed.

    Both current and former builds read this consumed bit. Writing it before
    the canonical authority commit can at worst burn a token if the later
    write fails; neither build can ever reuse it.
    """
    _refresh_legacy_paths()
    legacy = (
        _validate_tokens(read_json(_tokens_path(), _default_tokens()))
        if _tokens_path().exists()
        else _default_tokens()
    )
    legacy["tokens"][digest] = {
        "consumed": True,
        "consumed_at": time.time(),
        "node_id": node_id,
        "session_id": session_id,
    }
    atomic_write_json(_tokens_path(), legacy)
    mark_initialized(_tokens_path())


def _validate_nodes(payload: dict[str, Any]) -> dict[str, Any]:
    nodes = payload.get("nodes")
    if not isinstance(nodes, dict):
        raise NodeStateError("Node state has no nodes mapping")
    try:
        next_node_seq = int(payload.get("next_node_seq", 1))
    except (TypeError, ValueError) as exc:
        raise NodeStateError("Node state has an invalid sequence") from exc
    if next_node_seq < 1:
        raise NodeStateError("Node state has an invalid sequence")
    for node_id, node in nodes.items():
        if not isinstance(node_id, str) or not isinstance(node, dict):
            raise NodeStateError("Node state has an invalid Node record")
        if node.get("node_id") != node_id:
            raise NodeStateError("Node state has an inconsistent Node id")
        if not isinstance(node.get("session_id"), str) or not node.get("session_id"):
            raise NodeStateError("Node state has an invalid Session binding")
        access_digest = node.get("access_token_sha256")
        if not _is_sha256(access_digest):
            raise NodeStateError("Node state has an invalid access token digest")
        if "session_assigned" in node and not isinstance(node["session_assigned"], bool):
            raise NodeStateError("Node state has an invalid assignment flag")
    payload["next_node_seq"] = next_node_seq
    return payload


def _validate_tokens(payload: dict[str, Any]) -> dict[str, Any]:
    token_records = payload.get("tokens")
    if not isinstance(token_records, dict):
        raise NodeStateError("bootstrap token state has no tokens mapping")
    nodes = payload.get("nodes") if isinstance(payload.get("nodes"), dict) else None
    canonical_fields = {
        "bootstrap_request_id",
        "provider_server_id",
        "boot_id",
        "node_access_token_sha256",
    }
    for digest, record in token_records.items():
        if not _is_sha256(digest) or not isinstance(record, dict):
            raise NodeStateError("bootstrap token state has an invalid record")
        if record.get("consumed") is not True:
            # Ambiguous or reverted consumption must never become reusable.
            raise NodeStateError("bootstrap token state has an invalid consumed flag")
        consumed_at = record.get("consumed_at")
        if (
            isinstance(consumed_at, bool)
            or not isinstance(consumed_at, (int, float))
            or not math.isfinite(float(consumed_at))
            or float(consumed_at) < 0
        ):
            raise NodeStateError("bootstrap token state has an invalid timestamp")
        node_id = record.get("node_id")
        session_id = record.get("session_id")
        if not isinstance(node_id, str) or not node_id or not isinstance(session_id, str) or not session_id:
            raise NodeStateError("bootstrap token state has an invalid binding")

        present_canonical = canonical_fields.intersection(record)
        if present_canonical and present_canonical != canonical_fields:
            raise NodeStateError("bootstrap token state has a partial attempt identity")
        if present_canonical:
            for field in ("bootstrap_request_id", "provider_server_id", "boot_id"):
                if not isinstance(record.get(field), str) or not record.get(field):
                    raise NodeStateError("bootstrap token state has an invalid attempt identity")
            access_digest = record.get("node_access_token_sha256")
            if not _is_sha256(access_digest):
                raise NodeStateError("bootstrap token state has an invalid access digest")
            if nodes is not None:
                node = nodes.get(node_id)
                if not isinstance(node, dict):
                    raise NodeStateError("bootstrap token references a missing Node")
                if (
                    node.get("session_id") != session_id
                    or node.get("provider_server_id") != record.get("provider_server_id")
                    or node.get("boot_id") != record.get("boot_id")
                    or not secrets.compare_digest(
                        str(node.get("access_token_sha256", "")), str(access_digest)
                    )
                ):
                    raise NodeStateError("bootstrap token and Node authority disagree")
    return payload


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def configured_token_digests() -> set[str]:
    raw = os.getenv("NODE_BOOTSTRAP_TOKENS", "")
    return {hash_token(item.strip()) for item in raw.split(",") if item.strip()}


def configured_admin_token_digests() -> set[str]:
    values = [
        item.strip()
        for item in os.getenv("NODE_INTERNAL_ADMIN_TOKENS", "").split(",")
        if item.strip()
    ]
    token_file = os.getenv("NODE_INTERNAL_ADMIN_TOKEN_FILE", "").strip()
    if token_file:
        try:
            value = Path(token_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise HTTPException(
                status_code=503, detail="node admin authentication is unavailable"
            ) from exc
        if value:
            values.append(value)
    return {hash_token(value) for value in values}


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="empty bearer token")
    return token


def _require_node_access(node: dict[str, Any], authorization: str | None) -> None:
    supplied = hash_token(_bearer(authorization))
    expected = str(node.get("access_token_sha256", ""))
    if not expected or not secrets.compare_digest(expected, supplied):
        raise HTTPException(status_code=401, detail="invalid node access token")


def require_assigned_node(
    authorization: str | None,
    *,
    session_id: str,
) -> dict[str, Any]:
    """Authenticate one Node and bind an internal request to its Session."""
    supplied = hash_token(_bearer(authorization))
    with node_state_lock(exclusive=False):
        authority = _read_authority()
        matched: dict[str, Any] | None = None
        for node in authority["nodes"].values():
            expected = str(node.get("access_token_sha256", ""))
            if expected and secrets.compare_digest(expected, supplied):
                matched = node
                break
        if (
            matched is None
            or not bool(matched.get("session_assigned"))
            or str(matched.get("session_id") or "") != session_id
            or str(matched.get("desired_state", "RUNNING")) != "RUNNING"
        ):
            raise HTTPException(status_code=401, detail="invalid node access token")
        return dict(matched)


def _require_admin_access(authorization: str | None) -> None:
    supplied = hash_token(_bearer(authorization))
    configured = configured_admin_token_digests()
    if not configured or not any(
        secrets.compare_digest(expected, supplied) for expected in configured
    ):
        raise HTTPException(status_code=401, detail="invalid node admin token")


def _issue_node_id(state: dict[str, Any]) -> str:
    seq = int(state.get("next_node_seq", 1))
    node_id = f"node-{seq:04d}"
    state["next_node_seq"] = seq + 1
    return node_id


def _require_session_assignment() -> bool:
    configured = os.getenv("NODE_BOOTSTRAP_REQUIRE_SESSION_ASSIGNMENT")
    if configured is not None:
        return configured.strip().lower() not in {"0", "false", "no", "off"}
    return provider_mode() == "conoha"


def _resolve_assigned_session(provider_server_id: str) -> dict[str, Any] | None:
    matches = default_store().find_by_provider_server_id(
        provider_server_id,
        states=ACTIVE_STATES,
    )
    if len(matches) > 1:
        raise HTTPException(status_code=409, detail="provider server matches multiple sessions")
    if matches:
        return matches[0]
    if _require_session_assignment():
        raise HTTPException(status_code=409, detail="provider server is not assigned to an active session")
    return None


def _resolve_egress_delivery(
    assigned_session: dict[str, Any] | None,
) -> tuple[str, str | None]:
    """Resolve a Session Destination only for the bootstrap response.

    The fully credentialed URL is never copied into Session or Node state. The
    Node Agent immediately writes it into its tmpfs secret file.
    """
    if assigned_session is not None and assigned_session.get("egress_mode") == "RELAY_ONLY":
        return "", None

    if assigned_session is None or not assigned_session.get("destination_id"):
        return os.getenv("NODE_EGRESS_URL", ""), os.getenv("NODE_EGRESS_VERIFIED_PEER_IP")

    user_id = str(assigned_session.get("user_id", ""))
    destination_id = str(assigned_session["destination_id"])
    try:
        destination = store_get_destination(destination_id, user_id)
    except CatalogNotFound as exc:
        raise HTTPException(status_code=409, detail="session destination does not exist") from exc
    if destination.get("enabled", True) is not True:
        raise HTTPException(status_code=409, detail="session destination is disabled")
    if destination.get("verification_status") != "VERIFIED":
        raise HTTPException(status_code=409, detail="session destination is not verified")

    try:
        secret = default_destination_secret_store().resolve(
            user_id=user_id,
            secret_ref=str(destination.get("secret_ref", "")),
        )
    except DestinationSecretConfigurationError as exc:
        raise HTTPException(
            status_code=503, detail="destination secret store is not configured"
        ) from exc
    except DestinationSecretNotFound as exc:
        raise HTTPException(status_code=409, detail="session destination secret is missing") from exc
    except DestinationSecretError as exc:
        raise HTTPException(status_code=500, detail="session destination secret is unavailable") from exc

    try:
        transport = destination.get("verification_transport") or {}
        peer_ip = str(transport.get("peer_ip") or "").strip() or None
        return build_egress_url(destination, secret), peer_ip
    except EgressDestinationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _resolve_egress_url(assigned_session: dict[str, Any] | None) -> str:
    """Backward-compatible URL-only helper for callers that do not deliver secrets."""
    return _resolve_egress_delivery(assigned_session)[0]


def _ingest_event_types(
    previous: object,
    current: dict[str, Any],
    *,
    had_connection: bool = False,
) -> list[str]:
    previous = previous if isinstance(previous, dict) else {}
    previous_status = previous.get("status")
    previous_source = previous.get("source_id")
    current_status = current.get("status")
    current_source = current.get("source_id")
    previous_online = bool(previous.get("online", False))
    current_online = bool(current.get("online", False))

    events: list[str] = []
    if previous_online and not current_online:
        events.append("ingest.disconnected")
        return events

    if current_online and not previous_online:
        events.append("ingest.reconnected" if had_connection else "ingest.connected")
        events.append("ingest.format_detected")
    elif current_online and previous_source != current_source:
        events.append("ingest.reconnected" if had_connection else "ingest.connected")
        events.append("ingest.format_detected")

    if current_status == "REJECTED" and previous_status != "REJECTED":
        events.append("ingest.rejected")
    elif current_status == "DEGRADED" and previous_status != "DEGRADED":
        events.append("ingest.degraded")
    elif (
        previous_status == "DEGRADED"
        and current_online
        and current_status in {"PENDING", "ACCEPTED", "WARNING"}
    ):
        events.append("ingest.recovered")
    elif current_status != previous_status and not events:
        events.append("ingest.policy_changed")

    return events


def _append_ingest_events(
    node: dict[str, Any], previous: object, current: dict[str, Any]
) -> list[str]:
    event_types = _ingest_event_types(
        previous,
        current,
        had_connection=bool(node.get("ingest_ever_online", False)),
    )
    if not event_types:
        return []
    events = list(node.get("events", []))
    next_sequence = int(node.get("next_event_seq", len(events) + 1))
    for event_type in event_types:
        events.append(
            {
                "sequence": next_sequence,
                "type": event_type,
                "occurred_at": time.time(),
                "payload": {
                    "status": current.get("status"),
                    "source_type": current.get("source_type"),
                    "source_id": current.get("source_id"),
                    "bitrate_bps": current.get("bitrate_bps"),
                    "tracks": current.get("tracks", []),
                    "quality": current.get("quality"),
                    "reasons": current.get("reasons", []),
                    "warnings": current.get("warnings", []),
                    "enforced": current.get("enforced", False),
                },
            }
        )
        next_sequence += 1
    node["events"] = events[-100:]
    node["next_event_seq"] = next_sequence
    if current.get("online"):
        node["ingest_ever_online"] = True
    return event_types


def _egress_event_types(
    previous: object,
    current: dict[str, Any],
    *,
    had_connection: bool = False,
) -> list[str]:
    previous = previous if isinstance(previous, dict) else {}
    previous_status = str(previous.get("status", ""))
    current_status = str(current.get("status", ""))
    previous_connected = bool(previous.get("connected", False))
    current_connected = bool(current.get("connected", False))

    events: list[str] = []
    if current_status == previous_status and current_connected == previous_connected:
        return events

    if current_status == "STARTING":
        events.append("egress.starting")
        return events

    if current_connected and current_status == "CONNECTED":
        events.append("egress.recovered" if had_connection else "egress.connected")
        return events

    if previous_connected and current_status != "STOPPED":
        events.append("egress.disconnected")

    mapping = {
        "RECONNECTING": "egress.reconnecting",
        "AUTH_FAILED": "egress.auth_failed",
        "FAILED": "egress.failed",
        "STOPPED": "egress.stopped",
    }
    event_type = mapping.get(current_status)
    if event_type:
        events.append(event_type)
    return events


def _egress_payload(node_id: str, current: dict[str, Any]) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "status": current.get("status"),
        "connected": bool(current.get("connected", False)),
        "attempt": current.get("attempt"),
        "reason_code": current.get("reason_code"),
        "rendered_buffers": current.get("rendered_buffers"),
        "next_retry_at": current.get("next_retry_at"),
        "destination_scheme": current.get("destination_scheme"),
        "destination_host": current.get("destination_host"),
        "observed_at": current.get("observed_at"),
    }


def _append_egress_events(
    node: dict[str, Any], previous: object, current: dict[str, Any]
) -> list[str]:
    event_types = _egress_event_types(
        previous,
        current,
        had_connection=bool(node.get("egress_ever_connected", False)),
    )
    if not event_types:
        return []
    events = list(node.get("events", []))
    next_sequence = int(node.get("next_event_seq", len(events) + 1))
    payload = _egress_payload(str(node.get("node_id", "")), current)
    for event_type in event_types:
        events.append(
            {
                "sequence": next_sequence,
                "type": event_type,
                "occurred_at": time.time(),
                "payload": dict(payload),
            }
        )
        next_sequence += 1
    node["events"] = events[-100:]
    node["next_event_seq"] = next_sequence
    if current.get("connected") and current.get("status") == "CONNECTED":
        node["egress_ever_connected"] = True
    return event_types


def _apply_egress_to_session(
    *,
    session_id: str,
    node_id: str,
    event_types: list[str],
    current: dict[str, Any],
) -> None:
    store = default_store()
    session = store.get(session_id)
    if session is None:
        raise KeyError(session_id)
    if session.get("node_id") not in {None, node_id}:
        raise RuntimeError("node is not assigned to session")
    if session.get("status") not in ACTIVE_STATES:
        return
    payload = _egress_payload(node_id, current)
    reason = payload.get("reason_code")
    for event_type in event_types:
        store.append_event(
            session_id,
            event_type=event_type,
            reason_code=str(reason)[:100] if reason else None,
            payload=dict(payload),
            origin="node-agent",
        )
    store.update(
        session_id,
        egress_status=current.get("status"),
        egress_connected=bool(current.get("connected", False)),
        egress_last_reason=current.get("reason_code"),
        egress_updated_at=current.get("observed_at"),
    )


def _append_relay_client_events(
    node: dict[str, Any], previous: object, current: dict[str, Any]
) -> list[str]:
    previous = previous if isinstance(previous, dict) else {}
    previous_connected = bool(previous.get("connected", False))
    current_connected = bool(current.get("connected", False))
    event_types: list[str] = []
    if current_connected and not previous_connected:
        event_types.append(
            "relay.client.reconnected"
            if node.get("relay_client_ever_connected", False)
            else "relay.client.connected"
        )
    elif not current_connected and previous_connected:
        event_types.append("relay.client.disconnected")
    if not event_types:
        return []

    events = list(node.get("events", []))
    next_sequence = int(node.get("next_event_seq", len(events) + 1))
    payload = _relay_client_payload(str(node.get("node_id", "")), current)
    for event_type in event_types:
        events.append(
            {
                "sequence": next_sequence,
                "type": event_type,
                "occurred_at": time.time(),
                "payload": dict(payload),
            }
        )
        next_sequence += 1
    node["events"] = events[-100:]
    node["next_event_seq"] = next_sequence
    if current_connected:
        node["relay_client_ever_connected"] = True
    return event_types


def _relay_client_payload(node_id: str, current: dict[str, Any]) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "status": current.get("status"),
        "connected": bool(current.get("connected", False)),
        "reader_count": max(0, int(current.get("reader_count", 0) or 0)),
        "reason_code": current.get("reason_code"),
        "observed_at": current.get("observed_at"),
    }


def _apply_relay_client_to_session(
    *,
    session_id: str,
    node_id: str,
    event_types: list[str],
    current: dict[str, Any],
) -> None:
    default_store().apply_relay_client_observation(
        session_id,
        node_id=node_id,
        event_types=event_types,
        observation=current,
    )


router = APIRouter(prefix="/internal")


@router.post("/nodes/bootstrap")
def bootstrap(
    request: BootstrapRequest,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> dict[str, Any]:
    token = _bearer(authorization)
    digest = hash_token(token)
    if digest not in configured_token_digests():
        raise HTTPException(status_code=401, detail="unknown bootstrap token")

    with node_state_lock(exclusive=True):
        return _bootstrap_locked(request, digest)


def _bootstrap_locked(request: BootstrapRequest, digest: str) -> dict[str, Any]:
    """Consume one bootstrap token and create one Node under a single write."""
    authority = _read_authority()
    token_records = authority["tokens"]
    consumed = token_records.get(digest)
    if isinstance(consumed, dict) and consumed.get("consumed"):
        node = authority["nodes"].get(consumed.get("node_id"))
        supplied_access_digest = hash_token(request.node_access_token)
        same_attempt = bool(
            isinstance(node, dict)
            and consumed.get("bootstrap_request_id") == request.bootstrap_request_id
            and consumed.get("provider_server_id") == request.provider_server_id
            and consumed.get("boot_id") == request.boot_id
            and secrets.compare_digest(
                str(consumed.get("node_access_token_sha256", "")),
                supplied_access_digest,
            )
            and secrets.compare_digest(
                str(node.get("access_token_sha256", "")),
                supplied_access_digest,
            )
        )
        if same_attempt:
            assigned_session = (
                default_store().get(str(node["session_id"]))
                if node.get("session_assigned")
                else None
            )
            if node.get("session_assigned") and assigned_session is None:
                raise HTTPException(
                    status_code=409,
                    detail="assigned Session is no longer available",
                )
            return _bootstrap_response(
                node, assigned_session, request.node_access_token
            )
        raise HTTPException(status_code=409, detail="bootstrap token already consumed")

    assigned_session = _resolve_assigned_session(request.provider_server_id)
    node_id = _issue_node_id(authority)
    session_id = (
        str(assigned_session["session_id"])
        if assigned_session is not None
        else str(uuid.uuid4())
    )
    configured_deadline = time.time() + float(
        os.getenv("NODE_ABSOLUTE_DEADLINE_HOURS", "12")
    ) * 3600
    if assigned_session is not None and assigned_session.get("absolute_deadline_at") is not None:
        try:
            absolute_deadline = float(assigned_session["absolute_deadline_at"])
        except (TypeError, ValueError):
            absolute_deadline = configured_deadline
    else:
        absolute_deadline = configured_deadline

    if assigned_session is not None:
        try:
            default_store().bind_node(
                session_id,
                node_id=node_id,
                boot_id=request.boot_id,
                provider_server_id=request.provider_server_id,
            )
        except (KeyError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    node_access_digest = hash_token(request.node_access_token)
    egress_mode = (
        "RELAY_ONLY"
        if assigned_session is not None
        and assigned_session.get("egress_mode") == "RELAY_ONLY"
        else "DIRECT_PUSH"
    )
    node = {
        "node_id": node_id,
        "session_id": session_id,
        "session_assigned": assigned_session is not None,
        "destination_id": (
            assigned_session.get("destination_id") if assigned_session is not None else None
        ),
        "egress_mode": egress_mode,
        "provider_server_id": request.provider_server_id,
        "boot_id": request.boot_id,
        "agent_version": request.agent_version,
        "public_address": request.public_address,
        "private_address": request.private_address,
        "status": "BOOTSTRAPPING",
        "desired_state": "RUNNING",
        "absolute_deadline": absolute_deadline,
        "last_heartbeat_at": None,
        "ingest": None,
        "ingest_ever_online": False,
        "egress": None,
        "egress_ever_connected": False,
        "relay_client": None,
        "relay_client_ever_connected": False,
        "events": [],
        "next_event_seq": 1,
        "created_at": time.time(),
        "access_token_sha256": node_access_digest,
    }
    authority["nodes"][node_id] = node

    # Node and token consumption share one atomic authority file. The Agent
    # chooses and retains its access token, so an identical retry can prove
    # ownership without the Control Plane persisting or reissuing a raw token.
    token_records[digest] = {
        "consumed": True,
        "consumed_at": time.time(),
        "node_id": node_id,
        "session_id": session_id,
        "bootstrap_request_id": request.bootstrap_request_id,
        "provider_server_id": request.provider_server_id,
        "boot_id": request.boot_id,
        "node_access_token_sha256": node_access_digest,
    }
    _write_legacy_token_fuse(digest, node_id=node_id, session_id=session_id)
    _write_authority(authority)

    return _bootstrap_response(node, assigned_session, request.node_access_token)


def _bootstrap_response(
    node: dict[str, Any],
    assigned_session: dict[str, Any] | None,
    node_access_token: str,
) -> dict[str, Any]:
    egress_mode = str(node.get("egress_mode", "DIRECT_PUSH"))
    egress_url, peer_ip = _resolve_egress_delivery(assigned_session)
    if egress_mode == "DIRECT_PUSH" and egress_url and not peer_ip:
        raise HTTPException(status_code=409, detail="verified destination peer IP is unavailable")

    return {
        "node_id": node["node_id"],
        "session_id": node["session_id"],
        "session_assigned": bool(node.get("session_assigned")),
        "status": "BOOTSTRAPPING",
        "absolute_deadline": node["absolute_deadline"],
        "egress_url": egress_url,
        "egress_verified_peer_ip": peer_ip,
        "audio_mode": os.getenv("NODE_INITIAL_AUDIO_MODE", "LIVE"),
        "audio_version": 0,
        "audio_command_id": None,
        "audio_idempotency_key": None,
        "audio_updated_at": time.time(),
        "egress_mode": egress_mode,
        "media_mtx_config_ref": "config/mediamtx.yml",
        "node_access_token": node_access_token,
    }


@router.post("/nodes/{node_id}/heartbeat")
def heartbeat(
    node_id: str,
    request: HeartbeatRequest,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> dict[str, Any]:
    with node_state_lock(exclusive=True):
        return _heartbeat_locked(node_id, request, authorization)


def _heartbeat_locked(
    node_id: str,
    request: HeartbeatRequest,
    authorization: str | None,
) -> dict[str, Any]:
    authority = _read_authority()
    node = authority["nodes"].get(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="unknown node")
    _require_node_access(node, authorization)

    observed_at = time.time()
    node["status"] = request.status
    node["last_heartbeat_at"] = observed_at
    node["media_health"] = request.media_health
    node["active_publisher"] = request.active_publisher
    node["egress_connected"] = request.egress_connected
    if request.software_version:
        node["software_version"] = request.software_version
    if request.cpu_percent is not None:
        node["cpu_percent"] = request.cpu_percent
    if request.memory_mb is not None:
        node["memory_mb"] = request.memory_mb
    if request.deadline_remaining_seconds is not None:
        node["deadline_remaining_seconds"] = request.deadline_remaining_seconds
    if node.get("session_assigned"):
        heartbeat_recorded = default_store().record_node_heartbeat(
            str(node["session_id"]),
            node_id=node_id,
            boot_id=str(node.get("boot_id") or ""),
            observed_at=observed_at,
            node_ready=(request.status == "READY" and request.media_health == "running"),
        )
        if not heartbeat_recorded:
            raise HTTPException(
                status_code=409,
                detail="assigned Session no longer accepts this Node heartbeat",
            )
    if request.ingest is not None:
        previous = node.get("ingest")
        current = request.ingest.model_dump()
        event_types = _append_ingest_events(node, previous, current)
        node["ingest"] = current
        if node.get("session_assigned"):
            try:
                default_store().apply_ingest_observation(
                    str(node["session_id"]),
                    node_id=node_id,
                    event_types=event_types,
                    observation=current,
                )
                node.pop("session_event_error", None)
            except (KeyError, RuntimeError) as exc:
                node["session_event_error"] = str(exc)[:200]
    if request.egress is not None:
        previous_egress = node.get("egress")
        current_egress = request.egress.model_dump()
        egress_event_types = _append_egress_events(
            node, previous_egress, current_egress
        )
        node["egress"] = current_egress
        node["egress_connected"] = bool(current_egress.get("connected", False))
        if node.get("session_assigned") and egress_event_types:
            try:
                _apply_egress_to_session(
                    session_id=str(node["session_id"]),
                    node_id=node_id,
                    event_types=egress_event_types,
                    current=current_egress,
                )
                node.pop("egress_session_event_error", None)
            except (KeyError, RuntimeError) as exc:
                node["egress_session_event_error"] = str(exc)[:200]
    if request.relay_client is not None:
        previous_relay = node.get("relay_client")
        current_relay = request.relay_client.model_dump()
        relay_event_types = _append_relay_client_events(
            node, previous_relay, current_relay
        )
        node["relay_client"] = current_relay
        if node.get("session_assigned") and relay_event_types:
            try:
                _apply_relay_client_to_session(
                    session_id=str(node["session_id"]),
                    node_id=node_id,
                    event_types=relay_event_types,
                    current=current_relay,
                )
                node.pop("relay_client_session_event_error", None)
            except (KeyError, RuntimeError) as exc:
                node["relay_client_session_event_error"] = str(exc)[:200]
    if node.get("session_assigned"):
        try:
            apply_pipeline_health(
                default_store(),
                node=node,
                node_id=node_id,
                session_id=str(node["session_id"]),
                node_status=request.status,
                media_health=request.media_health,
                observed_at=float(node["last_heartbeat_at"]),
            )
            node.pop("pipeline_session_event_error", None)
        except (KeyError, RuntimeError) as exc:
            node["pipeline_session_event_error"] = str(exc)[:200]
    _write_authority(authority)

    return {"desired_state": node.get("desired_state", "RUNNING")}


@router.post("/nodes/{node_id}/stop")
def stop_node(
    node_id: str,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> dict[str, Any]:
    _require_admin_access(authorization)
    with node_state_lock(exclusive=True):
        authority = _read_authority()
        node = authority["nodes"].get(node_id)
        if node is None:
            raise HTTPException(status_code=404, detail="unknown node")
        node["desired_state"] = "STOPPED"
        node["status"] = "STOPPING"
        _write_authority(authority)
        return {"node_id": node_id, "desired_state": "STOPPED"}


@router.get("/nodes")
def list_nodes(
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> dict[str, Any]:
    _require_admin_access(authorization)
    with node_state_lock(exclusive=False):
        authority = _read_authority()
        return {
            "next_node_seq": authority["next_node_seq"],
            "nodes": {
                node_id: {
                    key: value
                    for key, value in node.items()
                    if key != "access_token_sha256"
                }
                for node_id, node in authority.get("nodes", {}).items()
                if isinstance(node, dict)
            },
        }
