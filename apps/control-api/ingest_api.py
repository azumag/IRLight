"""User-facing ingest credentials and MediaMTX external publish authentication."""

from __future__ import annotations

import math
import os
import time
from typing import Annotated, Any, Iterable, Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from auth_api import require_csrf, require_user
from fake_provider_for_api import default_store
from ingest_auth_guard import default_ingest_auth_guard
from ingest_store import default_ingest_store
from node_internal import require_assigned_node


ACCEPTING_INGEST_STATES = {"READY_WAIT_INGEST", "LIVE", "DEGRADED", "HOLDING"}
INGEST_PATH = "live/input"
RELAY_PATH = "output/relay"
DEFAULT_AUTH_CACHE_MAX_AGE_SECONDS = 300.0


class IssueIngestCredentialRequest(BaseModel):
    # RTMPS shares MediaMTX's RTMP authentication protocol and therefore uses
    # the same RTMP credential permission instead of a separate stored value.
    protocols: list[Literal["rtmp", "srt"]] = Field(
        default_factory=lambda: ["rtmp", "srt"], min_length=1
    )
    ttl_seconds: int = Field(default=12 * 3600, ge=60, le=12 * 3600)
    scope: Literal["INGEST", "RELAY_CLIENT"] = "INGEST"


class MediaMTXAuthRequest(BaseModel):
    user: str = ""
    password: str = ""
    token: str = ""
    ip: str = ""
    action: str = ""
    path: str = ""
    protocol: str = ""
    id: str = ""
    query: str = ""
    userAgent: str = ""


user_router = APIRouter(prefix="/v1/sessions")
internal_router = APIRouter(prefix="/internal/ingest")

CurrentUser = Annotated[dict[str, Any], Depends(require_user)]
Csrf = Annotated[None, Depends(require_csrf)]


def _owned_session(session_id: str, user_id: str) -> dict[str, Any]:
    session = default_store().get(session_id)
    if session is None or session.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail="unknown session")
    return session


def _host_for_url(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def _public_ingest_host(session: dict[str, Any]) -> str:
    # A configured hostname wins over the provider IP. This matters for RTMPS:
    # certificates are issued for the stable DNS name while on-demand nodes can
    # receive a different public IP on every prepare.
    configured = os.getenv("IRLIGHT_INGEST_PUBLIC_HOST", "").strip()
    if configured:
        return configured
    return str(session.get("provider_public_ipv4") or "127.0.0.1")


def _connection_info(
    session: dict[str, Any],
    username: str,
    secret: str | None,
    protocols: Iterable[str] = ("rtmp", "srt"),
) -> dict[str, Any]:
    allowed = {str(value).lower() for value in protocols}
    host = _public_ingest_host(session)
    host_for_url = _host_for_url(host)
    rtmp_port = int(os.getenv("IRLIGHT_INGEST_RTMP_PORT", "1935"))
    rtmps_port = int(os.getenv("IRLIGHT_INGEST_RTMPS_PORT", "1936"))
    srt_port = int(os.getenv("IRLIGHT_INGEST_SRT_PORT", "8890"))
    rtmp_enabled = "rtmp" in allowed
    rtmps_enabled = rtmp_enabled and os.getenv("IRLIGHT_INGEST_RTMPS_ENABLED", "") == "1"
    srt_enabled = "srt" in allowed

    rtmp: dict[str, Any] = {
        "enabled": rtmp_enabled,
        "server_url": (
            f"rtmp://{host_for_url}:{rtmp_port}/{INGEST_PATH}"
            if rtmp_enabled
            else None
        ),
        "username": username if rtmp_enabled else None,
        "password": secret if rtmp_enabled else None,
        "password_available": rtmp_enabled and secret is not None,
    }
    rtmps: dict[str, Any] = {
        "enabled": rtmps_enabled,
        "server_url": (
            f"rtmps://{host_for_url}:{rtmps_port}/{INGEST_PATH}"
            if rtmps_enabled
            else None
        ),
        "username": username if rtmps_enabled else None,
        "password": secret if rtmps_enabled else None,
        "password_available": rtmps_enabled and secret is not None,
    }
    srt: dict[str, Any] = {
        "enabled": srt_enabled,
        "host": host if srt_enabled else None,
        "port": srt_port if srt_enabled else None,
        "streamid_template": (
            f"publish:{INGEST_PATH}:{username}:<credential-secret>"
            if srt_enabled
            else None
        ),
        "url": None,
    }
    if srt_enabled and secret is not None:
        srt["url"] = (
            f"srt://{host_for_url}:{srt_port}"
            f"?streamid=publish:{INGEST_PATH}:{username}:{secret}"
        )
    return {"rtmp": rtmp, "rtmps": rtmps, "srt": srt}


def _relay_connection_info(
    session: dict[str, Any],
    username: str,
    secret: str | None,
) -> dict[str, Any]:
    host = _public_ingest_host(session)
    host_for_url = _host_for_url(host)
    port = int(os.getenv("IRLIGHT_RELAY_RTMP_PORT", "1935"))
    return {
        "protocol": "rtmp",
        "server_url": f"rtmp://{host_for_url}:{port}/{RELAY_PATH}",
        "username": username,
        "password": secret,
        "password_available": secret is not None,
    }


def _auth_cache_max_age_seconds() -> float:
    try:
        value = float(
            os.getenv(
                "IRLIGHT_INGEST_AUTH_CACHE_MAX_AGE_SECONDS",
                str(DEFAULT_AUTH_CACHE_MAX_AGE_SECONDS),
            )
        )
    except (TypeError, ValueError):
        value = DEFAULT_AUTH_CACHE_MAX_AGE_SECONDS
    if not math.isfinite(value):
        value = DEFAULT_AUTH_CACHE_MAX_AGE_SECONDS
    return min(3600.0, max(1.0, value))


def _cache_valid_until(record: dict[str, Any], *, now: float | None = None) -> float:
    current = time.time() if now is None else now
    try:
        credential_expires_at = float(record.get("expires_at", current))
    except (TypeError, ValueError):
        credential_expires_at = current
    return min(credential_expires_at, current + _auth_cache_max_age_seconds())


def _raise_auth_blocked(decision: Any) -> None:
    retry_after = max(1, int(getattr(decision, "retry_after_seconds", 1) or 1))
    raise HTTPException(
        status_code=429,
        detail="ingest authentication temporarily blocked",
        headers={"Retry-After": str(retry_after)},
    )


def _record_session_auth_failure(request: MediaMTXAuthRequest, decision: Any) -> None:
    """Add a secret-free auth audit when the supplied username is a real Session."""
    store = default_store()
    session = store.get(request.user)
    append_event = getattr(store, "append_event", None)
    if session is None or not callable(append_event):
        return
    try:
        append_event(
            request.user,
            event_type="ingest.auth_failed",
            reason_code=(
                "RATE_LIMITED"
                if getattr(decision, "blocked", False)
                else "INVALID_CREDENTIAL"
            ),
            payload={
                "node_id": session.get("node_id"),
                "source_ip": request.ip[:128] if request.ip else None,
                "protocol": request.protocol.lower()[:32],
                "publisher_id": request.id[:128] if request.id else None,
                "locked_scopes": list(getattr(decision, "locked_scopes", ()) or ()),
            },
            origin="ingest-auth",
        )
    except KeyError:
        return


def _reject_invalid_ingest_credential(request: MediaMTXAuthRequest, guard: Any) -> None:
    decision = guard.record_failure(
        source_ip=request.ip,
        username=request.user,
        protocol=request.protocol,
        publisher_id=request.id,
    )
    _record_session_auth_failure(request, decision)
    if decision.blocked:
        _raise_auth_blocked(decision)
    raise HTTPException(status_code=401, detail="invalid ingest credential")


@user_router.post("/{session_id}/ingest-credentials")
def issue_ingest_credential(
    session_id: str,
    request: IssueIngestCredentialRequest,
    current_user: CurrentUser,
    _csrf: Csrf = None,
) -> dict[str, Any]:
    user_id = str(current_user["id"])
    session = _owned_session(session_id, user_id)
    status = str(session.get("status", ""))
    if status not in ACCEPTING_INGEST_STATES:
        raise HTTPException(status_code=409, detail="session is not ready to accept ingest")
    if request.scope == "RELAY_CLIENT" and request.protocols != ["rtmp"]:
        raise HTTPException(status_code=409, detail="RELAY_CLIENT supports only rtmp")
    if request.scope == "RELAY_CLIENT" and session.get("egress_mode") != "RELAY_ONLY":
        raise HTTPException(
            status_code=409,
            detail="relay client credentials require RELAY_ONLY mode",
        )

    ttl = float(request.ttl_seconds)
    deadline = session.get("absolute_deadline_at")
    if deadline is not None:
        try:
            remaining = float(deadline) - time.time()
        except (TypeError, ValueError):
            remaining = ttl
        if remaining <= 0:
            raise HTTPException(status_code=409, detail="session deadline has expired")
        ttl = min(ttl, remaining)

    record, secret = default_ingest_store().issue(
        session_id=session_id,
        user_id=user_id,
        scope=request.scope,
        protocols=request.protocols,
        ttl_seconds=ttl,
    )
    if request.scope == "RELAY_CLIENT":
        connection_info = _relay_connection_info(
            session, str(record["username"]), secret
        )
    else:
        connection_info = _connection_info(
            session,
            str(record["username"]),
            secret,
            record.get("protocols", []),
        )
    return {
        **record,
        # Returned once. The raw value is never persisted and cannot be
        # recovered later; issuing another credential rotates the old one out.
        "credential_secret": secret,
        "connection_info": connection_info,
    }


@user_router.get("/{session_id}/connection-info")
def get_connection_info(session_id: str, current_user: CurrentUser) -> dict[str, Any]:
    user_id = str(current_user["id"])
    session = _owned_session(session_id, user_id)
    record = default_ingest_store().active_for_session(session_id)
    if record is None or record.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail="no active ingest credential")
    return {
        "credential": record,
        "connection_info": _connection_info(
            session,
            str(record["username"]),
            None,
            record.get("protocols", []),
        ),
    }


@user_router.get("/{session_id}/relay-client-info")
def get_relay_client_info(session_id: str, current_user: CurrentUser) -> dict[str, Any]:
    user_id = str(current_user["id"])
    session = _owned_session(session_id, user_id)
    if session.get("egress_mode") != "RELAY_ONLY":
        raise HTTPException(status_code=409, detail="session is not in RELAY_ONLY mode")
    record = default_ingest_store().active_for_session(
        session_id,
        scope="RELAY_CLIENT",
    )
    if record is None or record.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail="no active relay client credential")
    return {
        "credential": record,
        "connection_info": _relay_connection_info(
            session,
            str(record["username"]),
            None,
        ),
    }


@user_router.delete("/{session_id}/ingest-credentials/{credential_id}")
def revoke_ingest_credential(
    session_id: str,
    credential_id: str,
    current_user: CurrentUser,
    _csrf: Csrf = None,
) -> dict[str, Any]:
    user_id = str(current_user["id"])
    _owned_session(session_id, user_id)
    store = default_ingest_store()
    record = store.get(credential_id)
    if (
        record is None
        or record.get("session_id") != session_id
        or record.get("user_id") != user_id
        or record.get("scope", "INGEST") not in {"INGEST", "RELAY_CLIENT"}
    ):
        raise HTTPException(status_code=404, detail="unknown ingest credential")
    try:
        return store.revoke(credential_id, user_id=user_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="unknown ingest credential") from exc


@internal_router.post("/authorize")
@internal_router.post("/auth")
def authorize_mediamtx_request(
    request: MediaMTXAuthRequest,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> dict[str, Any]:
    """Authenticate a MediaMTX publish or relay read request.

    This endpoint is intended for MediaMTX ``authMethod: http`` and must be
    reachable only from trusted Media Nodes / internal networks. The response
    deliberately uses the same generic failure for unknown users, wrong
    secrets, expired credentials and stopped Sessions.

    MediaMTX reports both plain RTMP and TLS-wrapped RTMPS as protocol ``rtmp``;
    TLS policy is enforced by the MediaMTX listener configuration.
    """
    require_assigned_node(authorization, session_id=request.user)
    return _authorize_mediamtx_request(request)


def _authorize_mediamtx_request(request: MediaMTXAuthRequest) -> dict[str, Any]:
    if request.action not in {"publish", "read"}:
        raise HTTPException(status_code=403, detail="unsupported media action")
    if request.action == "read" and request.path != RELAY_PATH:
        raise HTTPException(status_code=403, detail="unsupported relay path")
    if request.action == "publish":
        if request.path != INGEST_PATH:
            raise HTTPException(status_code=403, detail="unsupported ingest path")
        expected_scope = "INGEST"
    else:
        expected_scope = "RELAY_CLIENT"
    protocol = request.protocol.lower()
    if protocol not in {"rtmp", "srt"}:
        raise HTTPException(status_code=403, detail="unsupported ingest protocol")
    if expected_scope == "RELAY_CLIENT" and protocol != "rtmp":
        raise HTTPException(status_code=403, detail="unsupported relay protocol")

    guard = default_ingest_auth_guard()
    decision = guard.check(source_ip=request.ip, username=request.user)
    if decision.blocked:
        guard.record_blocked(
            source_ip=request.ip,
            username=request.user,
            protocol=protocol,
            publisher_id=request.id,
        )
        _raise_auth_blocked(decision)

    session = default_store().get(request.user)
    if session is None or str(session.get("status", "")) not in ACCEPTING_INGEST_STATES:
        _reject_invalid_ingest_credential(request, guard)
    if expected_scope == "RELAY_CLIENT" and session.get("egress_mode") != "RELAY_ONLY":
        _reject_invalid_ingest_credential(request, guard)

    record = default_ingest_store().verify(
        username=request.user,
        secret=request.password,
        protocol=protocol,
        scope=expected_scope,
    )
    if record is None or record.get("session_id") != request.user:
        _reject_invalid_ingest_credential(request, guard)

    guard.record_success(username=request.user)
    return {
        "authorized": True,
        "session_id": request.user,
        "credential_id": record["id"],
        # Node-local auth proxies can use a previously successful decision only
        # during upstream transport/5xx failures, and never beyond this bound.
        "cache_valid_until": _cache_valid_until(record),
    }


def authorize_mediamtx_publish(request: MediaMTXAuthRequest) -> dict[str, Any]:
    """Backward-compatible alias for existing Control Plane callers."""
    return _authorize_mediamtx_request(request)
