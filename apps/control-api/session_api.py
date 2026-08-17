"""User-facing Session lifecycle API (Phase B spike)."""

from __future__ import annotations

import time
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from fake_provider_for_api import default_provider, default_store
from session_workflow import ProvisioningWorkflow


class PrepareRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)
    environment: str = Field(default="dev", pattern="^(dev|beta|prod)$")


class SessionEventRequest(BaseModel):
    type: str = Field(min_length=1, max_length=100)
    reason_code: str | None = Field(default=None, max_length=100)
    payload: dict[str, Any] = Field(default_factory=dict)


router = APIRouter(prefix="/v1/sessions")


@router.post("/{session_id}/prepare")
def prepare_session(
    session_id: str,
    request: PrepareRequest,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    key = idempotency_key or str(uuid.uuid4())
    if len(key) > 200:
        raise HTTPException(status_code=400, detail="Idempotency-Key is too long")

    store = default_store()
    existing = store.get(session_id)
    if existing is not None and existing.get("idempotency_key") == key:
        return existing

    workflow = ProvisioningWorkflow(store, default_provider())
    try:
        session = workflow.prepare(
            session_id,
            user_id=request.user_id,
            environment=request.environment,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"provisioning failed: {exc}") from exc

    store.update(session["session_id"], idempotency_key=key)
    return session


@router.post("/{session_id}/stop")
def stop_session(session_id: str) -> dict[str, Any]:
    store = default_store()
    workflow = ProvisioningWorkflow(store, default_provider())
    try:
        return workflow.stop(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="unknown session") from exc
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/{session_id}")
def get_session(session_id: str) -> dict[str, Any]:
    session = default_store().get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="unknown session")
    return session


@router.post("/{session_id}/events")
def add_session_event(session_id: str, request: SessionEventRequest) -> dict[str, Any]:
    store = default_store()
    session = store.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="unknown session")
    events = list(session.get("events", []))
    event = {
        "sequence": len(events) + 1,
        "type": request.type,
        "reason_code": request.reason_code,
        "payload": request.payload,
        "occurred_at": time.time(),
    }
    events.append(event)
    store.update(session_id, events=events)
    return event


@router.get("/{session_id}/events")
def list_session_events(session_id: str) -> dict[str, Any]:
    session = default_store().get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="unknown session")
    events = list(session.get("events", []))
    return {"session_id": session_id, "events": events}


@router.get("")
def list_sessions() -> dict[str, Any]:
    sessions = default_store().list()
    return {"sessions": sessions, "server_time": time.time()}