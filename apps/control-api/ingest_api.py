"""User-facing ingest credentials and MediaMTX external publish authentication."""

from __future__ import annotations

import os
import time
from typing import Annotated, Any, Iterable, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth_api import require_csrf, require_user
from fake_provider_for_api import default_store
from ingest_auth_guard import default_ingest_auth_guard
from ingest_store import default_ingest_store


ACCEPTING_INGEST_STATES = {"READY_WAIT_INGEST", "LIVE", "HOLDING"}
INGEST_PATH = "live/input"


class IssueIngestCredentialRequest(BaseModel):
    # RTMPS shares MediaMTX's RTMP authentication protocol and therefore uses
    # the same RTMP credential permission instead of a separate stored value.
    protocols: list[Literal["rtmp", "srt"]] = Field(
        default_factory=lambda: ["rtmp", "srt"], min_length=1
    )
    ttl_seconds: int = Field(default=12 * 3600, ge=60, le=12 * 3600)


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


def _raise_auth_blocked(decision: Any) -> None:
    retry_after = max(1, int(getattr(decision, "retry_after_seconds", 1) or 1))
    raise HTTPException(
        status_code=429,
        detail="ingest authentication temporarily blocked",
        headers={"Retry-After": str(retry_after)},
    )


def _reject_invalid_ingest_credential(request: MediaMTXAuthRequest, guard: Any) -> None:
    decision = guard.record_failure(
        source_ip=request.ip,
        username=request.user,
        protocol=request.protocol,
        publisher_id=request.id,
    )
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
        protocols=request.protocols,
        ttl_seconds=ttl,
    )
    return {
        **record,
        # Returned once. The raw value is never persisted and cannot be
        # recovered later; issuing another credential rotates the old one out.
        "credential_secret": secret,
        "connection_info": _connection_info(
            session, str(record["username"]), secret, record.get("protocols", [])
        ),
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
            session, str(record["username"]), None, record.get("protocols", [])
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
    active = store.active_for_session(session_id)
    if active is None or active.get("id") != credential_id or active.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail="unknown ingest credential")
    try:
        return store.revoke(credential_id, user_id=user_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="unknown ingest credential") from exc


@internal_router.post("/auth")
def authorize_mediamtx_publish(request: MediaMTXAuthRequest) -> dict[str, Any]:
    """Authenticate a MediaMTX publish request.

    This endpoint is intended for MediaMTX ``authMethod: http`` and must be
    reachable only from trusted Media Nodes / internal networks. The response
    deliberately uses the same generic failure for unknown users, wrong
    secrets, expired credentials and stopped Sessions.

    MediaMTX reports both plain RTMP and TLS-wrapped RTMPS as protocol ``rtmp``;
    TLS policy is enforced by the MediaMTX listener configuration.
    """
    if request.action != "publish":
        raise HTTPException(status_code=403, detail="unsupported media action")
    if request.path != INGEST_PATH:
        raise HTTPException(status_code=403, detail="unsupported ingest path")
    protocol = request.protocol.lower()
    if protocol not in {"rtmp", "srt"}:
        raise HTTPException(status_code=403, detail="unsupported ingest protocol")

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

    record = default_ingest_store().verify(
        username=request.user,
        secret=request.password,
        protocol=protocol,
    )
    if record is None or record.get("session_id") != request.user:
        _reject_invalid_ingest_credential(request, guard)

    guard.record_success(username=request.user)
    return {
        "authorized": True,
        "session_id": request.user,
        "credential_id": record["id"],
    }
