"""User-facing Session lifecycle API (Phase B spike)."""

from __future__ import annotations

import os
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from auth_api import require_csrf, require_user
from catalog_store import CatalogNotFound, get_destination as store_get_destination
from destination_secret_store import (
    DestinationSecretConfigurationError,
    DestinationSecretError,
    DestinationSecretNotFound,
    default_destination_secret_store,
)
from entitlement_store import default_entitlement_store
from fake_provider_for_api import default_provider, default_store, provider_mode
from ingest_store import default_ingest_store
from session_store import EntitlementExceeded
from session_workflow import ProvisioningWorkflow


class PrepareRequest(BaseModel):
    environment: str = Field(default="dev", pattern="^(dev|beta|prod)$")
    destination_id: str | None = Field(default=None, min_length=1, max_length=100)


class SessionEventRequest(BaseModel):
    type: str = Field(min_length=1, max_length=100)
    reason_code: str | None = Field(default=None, max_length=100)
    payload: dict[str, Any] = Field(default_factory=dict)


router = APIRouter(prefix="/v1/sessions")

CurrentUser = Annotated[dict[str, Any], Depends(require_user)]
Csrf = Annotated[None, Depends(require_csrf)]


def _owned_session(session_id: str, user_id: str) -> dict[str, Any]:
    session = default_store().get(session_id)
    if session is None or session.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail="unknown session")
    return session


def _destination_required() -> bool:
    configured = os.getenv("IRLIGHT_REQUIRE_DESTINATION")
    if configured is not None:
        return configured.strip().lower() not in {"0", "false", "no", "off"}
    return provider_mode() == "conoha"


def _validated_destination(destination_id: str | None, user_id: str) -> dict[str, Any] | None:
    if destination_id is None:
        if _destination_required():
            raise HTTPException(status_code=409, detail="destination_id is required")
        return None
    try:
        destination = store_get_destination(destination_id, user_id)
    except CatalogNotFound as exc:
        raise HTTPException(status_code=404, detail="unknown destination") from exc
    if destination.get("enabled", True) is not True:
        raise HTTPException(status_code=409, detail="destination is disabled")
    if str(destination.get("type", "")).lower() not in {"rtmp", "rtmps"}:
        raise HTTPException(status_code=409, detail="destination egress protocol is not supported")
    if destination.get("verification_status") != "VERIFIED":
        raise HTTPException(status_code=409, detail="destination must be verified before prepare")

    secret_ref = str(destination.get("secret_ref", ""))
    try:
        # Decrypt once before entitlement reservation/provider allocation so a
        # wrong master key or corrupt ciphertext cannot create a billable node.
        # The plaintext is intentionally not retained.
        default_destination_secret_store().resolve(
            user_id=user_id,
            secret_ref=secret_ref,
        )
    except DestinationSecretConfigurationError as exc:
        raise HTTPException(
            status_code=503, detail="destination secret store is not configured"
        ) from exc
    except DestinationSecretNotFound as exc:
        raise HTTPException(status_code=409, detail="destination secret is not configured") from exc
    except DestinationSecretError as exc:
        raise HTTPException(status_code=503, detail="destination secret is unavailable") from exc
    return destination


@router.post("/{session_id}/prepare")
def prepare_session(
    session_id: str,
    request: PrepareRequest,
    current_user: CurrentUser,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    _csrf: Csrf = None,
) -> dict[str, Any]:
    key = idempotency_key or str(uuid.uuid4())
    if len(key) > 200:
        raise HTTPException(status_code=400, detail="Idempotency-Key is too long")

    user_id = str(current_user["id"])
    store = default_store()
    existing = store.get(session_id)
    if existing is not None and existing.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail="unknown session")
    if existing is not None and existing.get("idempotency_key") == key:
        return existing

    destination = _validated_destination(request.destination_id, user_id)
    entitlement = default_entitlement_store().get(user_id)
    workflow = ProvisioningWorkflow(store, default_provider())
    try:
        store.reserve_prepare_slot(
            session_id,
            user_id=user_id,
            environment=request.environment,
            entitlement_id=str(entitlement["id"]),
            max_concurrent_sessions=int(entitlement["max_concurrent_sessions"]),
        )
        if destination is not None:
            store.update(session_id, destination_id=str(destination["id"]))
        session = workflow.prepare(
            session_id,
            user_id=user_id,
            environment=request.environment,
        )
    except EntitlementExceeded as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"provisioning failed: {exc}") from exc

    return store.update(session["session_id"], idempotency_key=key)


@router.post("/{session_id}/stop")
def stop_session(
    session_id: str, current_user: CurrentUser, _csrf: Csrf = None
) -> dict[str, Any]:
    _owned_session(session_id, str(current_user["id"]))
    store = default_store()
    workflow = ProvisioningWorkflow(store, default_provider())
    try:
        session = workflow.stop(session_id)
        default_ingest_store().revoke_session(session_id)
        return session
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="unknown session") from exc
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/{session_id}")
def get_session(session_id: str, current_user: CurrentUser) -> dict[str, Any]:
    return _owned_session(session_id, str(current_user["id"]))


@router.post("/{session_id}/events")
def add_session_event(
    session_id: str,
    request: SessionEventRequest,
    current_user: CurrentUser,
    _csrf: Csrf = None,
) -> dict[str, Any]:
    _owned_session(session_id, str(current_user["id"]))
    try:
        return default_store().append_event(
            session_id,
            event_type=request.type,
            reason_code=request.reason_code,
            payload=request.payload,
            origin="user-api",
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="unknown session") from exc


@router.get("/{session_id}/events")
def list_session_events(session_id: str, current_user: CurrentUser) -> dict[str, Any]:
    session = _owned_session(session_id, str(current_user["id"]))
    events = list(session.get("events", []))
    return {"session_id": session_id, "events": events}


@router.get("")
def list_sessions(current_user: CurrentUser) -> dict[str, Any]:
    user_id = str(current_user["id"])
    sessions = [s for s in default_store().list() if s.get("user_id") == user_id]
    return {"sessions": sessions}
