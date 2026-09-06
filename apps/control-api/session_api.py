"""User-facing Session lifecycle API (Phase B spike)."""

from __future__ import annotations

import os
import uuid
from typing import Annotated, Any, Literal

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
from egress_destination import EgressDestinationError, build_egress_url
from entitlement_store import EntitlementStateError, default_entitlement_store
from fake_provider_for_api import default_provider, default_store, provider_mode
from session_event_policy import (
    USER_EVENT_TYPE_RESERVED_CODE,
    UserEventPayloadError,
    UserEventTypeError,
    validate_user_event_payload,
    validate_user_event_type,
)
from session_store import SESSION_EVENT_LIMIT, EntitlementExceeded, SessionStateError
from session_workflow import ProvisioningWorkflow


ENTITLEMENT_STATE_UNAVAILABLE_CODE = "ENTITLEMENT_STATE_UNAVAILABLE"
SESSION_STATE_UNAVAILABLE_CODE = "SESSION_STATE_UNAVAILABLE"


class PrepareRequest(BaseModel):
    environment: str = Field(default="dev", pattern="^(dev|beta|prod)$")
    destination_id: str | None = Field(default=None, min_length=1, max_length=100)
    egress_mode: Literal["DIRECT_PUSH", "RELAY_ONLY"] = "DIRECT_PUSH"


class SessionEventRequest(BaseModel):
    type: str = Field(min_length=1, max_length=100)
    reason_code: str | None = Field(default=None, max_length=100)
    payload: dict[str, Any] = Field(default_factory=dict)


router = APIRouter(prefix="/v1/sessions")

CurrentUser = Annotated[dict[str, Any], Depends(require_user)]
Csrf = Annotated[None, Depends(require_csrf)]


def _session_state_unavailable(exc: SessionStateError) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={"code": SESSION_STATE_UNAVAILABLE_CODE},
    )


def _session_store():
    try:
        return default_store()
    except SessionStateError as exc:
        raise _session_state_unavailable(exc) from exc


def _owned_session(session_id: str, user_id: str) -> dict[str, Any]:
    store = _session_store()
    try:
        session = store.get(session_id)
    except SessionStateError as exc:
        raise _session_state_unavailable(exc) from exc
    if session is None or session.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail="unknown session")
    return session


def _destination_required() -> bool:
    configured = os.getenv("IRLIGHT_REQUIRE_DESTINATION")
    if configured is not None:
        return configured.strip().lower() not in {"0", "false", "no", "off"}
    return provider_mode() == "conoha"


def _validated_destination(
    destination_id: str | None,
    user_id: str,
    egress_mode: str = "DIRECT_PUSH",
) -> dict[str, Any] | None:
    if egress_mode == "RELAY_ONLY":
        if destination_id is not None:
            raise HTTPException(
                status_code=409,
                detail="destination_id must be omitted for RELAY_ONLY",
            )
        return None
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
        # Decrypt before entitlement reservation/provider allocation so a wrong
        # master key or corrupt ciphertext cannot create a billable node. Keep
        # the plaintext only long enough to validate the final publish URL; it
        # is never persisted in Session or returned by this API.
        secret = default_destination_secret_store().resolve(
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

    try:
        build_egress_url(destination, secret)
    except EgressDestinationError as exc:
        raise HTTPException(status_code=409, detail="destination configuration is invalid") from exc
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
    store = _session_store()
    try:
        prepared = store.get_prepare_replay(
            session_id,
            user_id=user_id,
            environment=request.environment,
            destination_id=request.destination_id,
            egress_mode=request.egress_mode,
            idempotency_key=key,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="unknown session") from exc
    except SessionStateError as exc:
        raise _session_state_unavailable(exc) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    # A committed response is replayable even if external Destination or
    # entitlement state changed after the original request. STOPPED is the
    # recoverable crash boundary between binding and provisioning claim.
    if prepared is not None and prepared.get("status") != "STOPPED":
        return prepared

    if prepared is None:
        destination = _validated_destination(
            request.destination_id, user_id, request.egress_mode
        )
        try:
            entitlement = default_entitlement_store().get(user_id)
        except EntitlementStateError as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": ENTITLEMENT_STATE_UNAVAILABLE_CODE},
            ) from exc
        try:
            prepared, replay = store.begin_prepare(
                session_id,
                user_id=user_id,
                environment=request.environment,
                entitlement_id=str(entitlement["id"]),
                max_concurrent_sessions=int(entitlement["max_concurrent_sessions"]),
                destination_id=(
                    str(destination["id"]) if destination is not None else None
                ),
                egress_mode=request.egress_mode,
                idempotency_key=key,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="unknown session") from exc
        except EntitlementExceeded as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except SessionStateError as exc:
            raise _session_state_unavailable(exc) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"provisioning failed: {exc}") from exc
        if replay and prepared.get("status") != "STOPPED":
            return prepared

    workflow = ProvisioningWorkflow(store, default_provider())
    try:
        session = workflow.prepare(
            session_id,
            user_id=user_id,
            environment=request.environment,
        )
    except SessionStateError as exc:
        raise _session_state_unavailable(exc) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"provisioning failed: {exc}") from exc

    return session


@router.post("/{session_id}/stop")
def stop_session(
    session_id: str, current_user: CurrentUser, _csrf: Csrf = None
) -> dict[str, Any]:
    _owned_session(session_id, str(current_user["id"]))
    store = _session_store()
    workflow = ProvisioningWorkflow(store, default_provider())
    try:
        return workflow.stop(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="unknown session") from exc
    except SessionStateError as exc:
        raise _session_state_unavailable(exc) from exc
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
        validate_user_event_type(request.type)
    except UserEventTypeError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": USER_EVENT_TYPE_RESERVED_CODE},
        ) from exc
    try:
        validate_user_event_payload(request.payload)
    except UserEventPayloadError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "USER_EVENT_PAYLOAD_INVALID"},
        ) from exc
    try:
        return _session_store().append_event(
            session_id,
            event_type=request.type,
            reason_code=request.reason_code,
            payload=request.payload,
            origin="user-api",
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="unknown session") from exc
    except SessionStateError as exc:
        raise _session_state_unavailable(exc) from exc


def _event_page(
    session: dict[str, Any],
    *,
    after_sequence: int | None,
    limit: int,
) -> dict[str, Any]:
    if after_sequence is not None and after_sequence < 0:
        raise HTTPException(status_code=400, detail="after_sequence must be non-negative")
    if limit < 1 or limit > SESSION_EVENT_LIMIT:
        raise HTTPException(
            status_code=400,
            detail=f"limit must be between 1 and {SESSION_EVENT_LIMIT}",
        )

    events = list(session.get("events", []))
    earliest_sequence = int(events[0]["sequence"]) if events else None
    latest_sequence = int(events[-1]["sequence"]) if events else None
    retention_gap = bool(
        after_sequence is not None
        and earliest_sequence is not None
        and after_sequence < earliest_sequence - 1
    )

    if after_sequence is None:
        remaining = events
    else:
        remaining = [
            event for event in events if int(event["sequence"]) > after_sequence
        ]
    page = remaining[:limit]

    return {
        "events": page,
        "earliest_sequence": earliest_sequence,
        "latest_sequence": latest_sequence,
        "next_after_sequence": (
            int(page[-1]["sequence"]) if page else after_sequence
        ),
        "has_more": len(remaining) > len(page),
        "retention_gap": retention_gap,
    }


@router.get("/{session_id}/events")
def list_session_events(
    session_id: str,
    current_user: CurrentUser,
    after_sequence: int | None = None,
    limit: int = SESSION_EVENT_LIMIT,
) -> dict[str, Any]:
    session = _owned_session(session_id, str(current_user["id"]))
    return {"session_id": session_id, **_event_page(
        session, after_sequence=after_sequence, limit=limit
    )}


@router.get("")
def list_sessions(current_user: CurrentUser) -> dict[str, Any]:
    user_id = str(current_user["id"])
    store = _session_store()
    try:
        sessions = [s for s in store.list() if s.get("user_id") == user_id]
    except SessionStateError as exc:
        raise _session_state_unavailable(exc) from exc
    return {"sessions": sessions}
