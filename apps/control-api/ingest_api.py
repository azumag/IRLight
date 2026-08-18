"""User-facing ingest credentials and MediaMTX external publish authentication."""

from __future__ import annotations

import os
import time
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth_api import require_csrf, require_user
from fake_provider_for_api import default_store
from ingest_store import default_ingest_store


ACCEPTING_INGEST_STATES = {"READY_WAIT_INGEST", "LIVE", "HOLDING"}
INGEST_PATH = "live/input"


class IssueIngestCredentialRequest(BaseModel):
    protocols: list[Literal["rtmp", "srt"]] = Field(default_factory=lambda: ["rtmp", "srt"])
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


def _connection_info(session: dict[str, Any], username: str, secret: str | None) -> dict[str, Any]:
    host = str(
        session.get("provider_public_ipv4")
        or os.getenv("IRLIGHT_INGEST_PUBLIC_HOST", "127.0.0.1")
    )
    host_for_url = _host_for_url(host)
    rtmp_port = int(os.getenv("IRLIGHT_INGEST_RTMP_PORT", "1935"))
    srt_port = int(os.getenv("IRLIGHT_INGEST_SRT_PORT", "8890"))

    rtmp: dict[str, Any] = {
        "server_url": f"rtmp://{host_for_url}:{rtmp_port}/{INGEST_PATH}",
        "username": username,
        "password": secret,
        "password_available": secret is not None,
    }
    srt: dict[str, Any] = {
        "host": host,
        "port": srt_port,
        "streamid_template": f"publish:{INGEST_PATH}:{username}:<credential-secret>",
        "url": None,
    }
    if secret is not None:
        srt["url"] = (
            f"srt://{host_for_url}:{srt_port}"
            f"?streamid=publish:{INGEST_PATH}:{username}:{secret}"
        )
    return {"rtmp": rtmp, "srt": srt}


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
        "connection_info": _connection_info(session, str(record["username"]), secret),
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
        "connection_info": _connection_info(session, str(record["username"]), None),
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
    """
    if request.action != "publish":
        raise HTTPException(status_code=403, detail="unsupported media action")
    if request.path != INGEST_PATH:
        raise HTTPException(status_code=403, detail="unsupported ingest path")
    protocol = request.protocol.lower()
    if protocol not in {"rtmp", "srt"}:
        raise HTTPException(status_code=403, detail="unsupported ingest protocol")

    session = default_store().get(request.user)
    if session is None or str(session.get("status", "")) not in ACCEPTING_INGEST_STATES:
        raise HTTPException(status_code=401, detail="invalid ingest credential")

    record = default_ingest_store().verify(
        username=request.user,
        secret=request.password,
        protocol=protocol,
    )
    if record is None or record.get("session_id") != request.user:
        raise HTTPException(status_code=401, detail="invalid ingest credential")

    return {
        "authorized": True,
        "session_id": request.user,
        "credential_id": record["id"],
    }
