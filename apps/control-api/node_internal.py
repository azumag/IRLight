"""Internal APIs used by Node Agents (bootstrap / heartbeat / stop).

These endpoints are intentionally separate from the user-facing API. They are
only reachable from inside the trusted network in the production compose file;
they must never be exposed to the public internet.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from catalog_store import CatalogNotFound, get_destination as store_get_destination
from destination_secret_store import (
    DestinationSecretConfigurationError,
    DestinationSecretError,
    DestinationSecretNotFound,
    default_destination_secret_store,
)
from egress_destination import EgressDestinationError, build_egress_url
from fake_provider_for_api import default_store, provider_mode
from session_store import ACTIVE_STATES


STATE_DIR = Path(os.getenv("NODE_STATE_DIR", "/state"))
NODES_PATH = STATE_DIR / "nodes.json"
TOKENS_PATH = STATE_DIR / "bootstrap_tokens.json"


class BootstrapRequest(BaseModel):
    provider_server_id: str = Field(min_length=1, max_length=200)
    boot_id: str = Field(min_length=1, max_length=200)
    agent_version: str = Field(min_length=1, max_length=100)
    public_address: str | None = Field(default=None, max_length=200)
    private_address: str | None = Field(default=None, max_length=200)


class IngestObservationRequest(BaseModel):
    status: str = Field(
        pattern="^(OFFLINE|UNKNOWN|PENDING|ACCEPTED|WARNING|DEGRADED|REJECTED)$"
    )
    path: str = Field(min_length=1, max_length=200)
    online: bool = False
    source_type: str | None = Field(default=None, max_length=50)
    source_id: str | None = Field(default=None, max_length=200)
    bitrate_bps: float | None = Field(default=None, ge=0)
    max_bitrate_bps: int | None = Field(default=None, ge=0)
    tracks: list[dict[str, Any]] = Field(default_factory=list, max_length=8)
    reasons: list[str] = Field(default_factory=list, max_length=16)
    warnings: list[str] = Field(default_factory=list, max_length=16)
    quality: dict[str, Any] | None = None
    enforced: bool = False
    enforcement_error: str | None = Field(default=None, max_length=200)
    observed_at: float


class EgressObservationRequest(BaseModel):
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


class HeartbeatRequest(BaseModel):
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


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else default
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _default_nodes() -> dict[str, Any]:
    return {"nodes": {}, "next_node_seq": 1}


def _default_tokens() -> dict[str, Any]:
    return {"tokens": {}}


def ensure_state() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not NODES_PATH.exists():
        atomic_write_json(NODES_PATH, _default_nodes())
    if not TOKENS_PATH.exists():
        atomic_write_json(TOKENS_PATH, _default_tokens())


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def configured_token_digests() -> set[str]:
    raw = os.getenv("NODE_BOOTSTRAP_TOKENS", "")
    return {hash_token(item.strip()) for item in raw.split(",") if item.strip()}


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


def _resolve_egress_url(assigned_session: dict[str, Any] | None) -> str:
    """Resolve a Session Destination only for the bootstrap response.

    The fully credentialed URL is never copied into Session or Node state. The
    Node Agent immediately writes it into its tmpfs secret file.
    """
    if assigned_session is None or not assigned_session.get("destination_id"):
        return os.getenv("NODE_EGRESS_URL", "")

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
        return build_egress_url(destination, secret)
    except EgressDestinationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


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


router = APIRouter(prefix="/internal")


@router.post("/nodes/bootstrap")
def bootstrap(
    request: BootstrapRequest,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> dict[str, Any]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="empty bearer token")
    digest = hash_token(token)
    if digest not in configured_token_digests():
        raise HTTPException(status_code=401, detail="unknown bootstrap token")

    tokens = read_json(TOKENS_PATH, _default_tokens())
    if tokens["tokens"].get(digest, {}).get("consumed"):
        raise HTTPException(status_code=409, detail="bootstrap token already consumed")

    assigned_session = _resolve_assigned_session(request.provider_server_id)
    egress_url = _resolve_egress_url(assigned_session)
    nodes = read_json(NODES_PATH, _default_nodes())
    node_id = _issue_node_id(nodes)
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

    node = {
        "node_id": node_id,
        "session_id": session_id,
        "session_assigned": assigned_session is not None,
        "destination_id": (
            assigned_session.get("destination_id") if assigned_session is not None else None
        ),
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
        "events": [],
        "next_event_seq": 1,
        "created_at": time.time(),
    }
    nodes["nodes"][node_id] = node
    atomic_write_json(NODES_PATH, nodes)

    tokens["tokens"][digest] = {
        "consumed": True,
        "consumed_at": time.time(),
        "node_id": node_id,
        "session_id": session_id,
    }
    atomic_write_json(TOKENS_PATH, tokens)

    return {
        "node_id": node_id,
        "session_id": session_id,
        "session_assigned": assigned_session is not None,
        "status": "BOOTSTRAPPING",
        "absolute_deadline": absolute_deadline,
        "egress_url": egress_url,
        "media_mtx_config_ref": "config/mediamtx.yml",
    }


@router.post("/nodes/{node_id}/heartbeat")
def heartbeat(
    node_id: str,
    request: HeartbeatRequest,
) -> dict[str, Any]:
    nodes = read_json(NODES_PATH, _default_nodes())
    node = nodes["nodes"].get(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="unknown node")

    node["status"] = request.status
    node["last_heartbeat_at"] = time.time()
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
    if request.ingest is not None:
        previous = node.get("ingest")
        current = request.ingest.model_dump()
        event_types = _append_ingest_events(node, previous, current)
        node["ingest"] = current
        if node.get("session_assigned") and event_types:
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
    atomic_write_json(NODES_PATH, nodes)

    return {"desired_state": node.get("desired_state", "RUNNING")}


@router.post("/nodes/{node_id}/stop")
def stop_node(node_id: str) -> dict[str, Any]:
    nodes = read_json(NODES_PATH, _default_nodes())
    node = nodes["nodes"].get(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="unknown node")
    node["desired_state"] = "STOPPED"
    node["status"] = "STOPPING"
    atomic_write_json(NODES_PATH, nodes)
    return {"node_id": node_id, "desired_state": "STOPPED"}


@router.get("/nodes")
def list_nodes() -> dict[str, Any]:
    nodes = read_json(NODES_PATH, _default_nodes())
    return nodes
