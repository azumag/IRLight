"""FastAPI routes for the Destination / Asset catalog."""

from __future__ import annotations

import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth_api import require_csrf, require_user
from catalog_store import (
    CatalogNotFound,
    CatalogValidationError,
    CatalogVerifyFailed,
    create_asset as store_create_asset,
    create_destination as store_create_destination,
    delete_asset as store_delete_asset,
    delete_destination as store_delete_destination,
    get_asset as store_get_asset,
    get_destination as store_get_destination,
    list_assets as store_list_assets,
    list_destinations as store_list_destinations,
    update_destination as store_update_destination,
    verify_destination as store_verify_destination,
)
from destination_secret_store import (
    DestinationSecretConfigurationError,
    default_destination_secret_store,
)


class DestinationCreate(BaseModel):
    platform: str = Field(default="custom", pattern="^(twitch|youtube|kick|custom)$")
    type: str = Field(default="rtmp", pattern="^(rtmp|rtmps|srt)$")
    display_name: str = Field(min_length=1, max_length=128)
    server_url: str = Field(min_length=1, max_length=500)
    secret_ref: str = Field(min_length=1, max_length=200)


class DestinationUpdate(BaseModel):
    platform: str | None = Field(
        default=None, pattern="^(twitch|youtube|kick|custom)$"
    )
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    server_url: str | None = Field(default=None, min_length=1, max_length=500)
    secret_ref: str | None = Field(default=None, min_length=1, max_length=200)
    enabled: bool | None = None


class DestinationSecretUpdate(BaseModel):
    value: str = Field(min_length=1, max_length=1000)


class AssetCreate(BaseModel):
    source_object_key: str = Field(min_length=1, max_length=500)


router = APIRouter(prefix="/v1")

CurrentUser = Annotated[dict[str, Any], Depends(require_user)]
Csrf = Annotated[None, Depends(require_csrf)]


def _not_found(exc: CatalogNotFound) -> HTTPException:
    return HTTPException(status_code=404, detail="unknown item")


def _bad_destination(exc: CatalogValidationError) -> HTTPException:
    return HTTPException(status_code=422, detail=str(exc))


def _secret_store_unavailable(exc: Exception) -> HTTPException:
    return HTTPException(status_code=503, detail="destination secret store is not configured")


@router.post("/destinations")
def create_destination(
    request: DestinationCreate, current_user: CurrentUser, _csrf: Csrf = None
) -> dict[str, Any]:
    try:
        return store_create_destination(
            user_id=str(current_user["id"]),
            platform=request.platform,
            type=request.type,
            display_name=request.display_name,
            server_url=request.server_url,
            secret_ref=request.secret_ref,
        )
    except CatalogValidationError as exc:
        raise _bad_destination(exc) from exc


@router.get("/destinations")
def list_destinations(current_user: CurrentUser) -> dict[str, Any]:
    return {
        "destinations": store_list_destinations(str(current_user["id"])),
        "server_time": time.time(),
    }


@router.get("/destinations/{destination_id}")
def get_destination(destination_id: str, current_user: CurrentUser) -> dict[str, Any]:
    try:
        return store_get_destination(destination_id, str(current_user["id"]))
    except CatalogNotFound as exc:
        raise _not_found(exc) from exc


@router.put("/destinations/{destination_id}")
def update_destination(
    destination_id: str,
    request: DestinationUpdate,
    current_user: CurrentUser,
    _csrf: Csrf = None,
) -> dict[str, Any]:
    try:
        return store_update_destination(
            destination_id,
            user_id=str(current_user["id"]),
            platform=request.platform,
            display_name=request.display_name,
            server_url=request.server_url,
            secret_ref=request.secret_ref,
            enabled=request.enabled,
        )
    except CatalogNotFound as exc:
        raise _not_found(exc) from exc
    except CatalogValidationError as exc:
        raise _bad_destination(exc) from exc


@router.put("/destinations/{destination_id}/secret")
def put_destination_secret(
    destination_id: str,
    request: DestinationSecretUpdate,
    current_user: CurrentUser,
    _csrf: Csrf = None,
) -> dict[str, Any]:
    user_id = str(current_user["id"])
    try:
        destination = store_get_destination(destination_id, user_id)
    except CatalogNotFound as exc:
        raise _not_found(exc) from exc
    try:
        return default_destination_secret_store().put(
            user_id=user_id,
            secret_ref=str(destination["secret_ref"]),
            value=request.value,
        )
    except DestinationSecretConfigurationError as exc:
        raise _secret_store_unavailable(exc) from exc


@router.delete("/destinations/{destination_id}/secret")
def delete_destination_secret(
    destination_id: str,
    current_user: CurrentUser,
    _csrf: Csrf = None,
) -> dict[str, Any]:
    user_id = str(current_user["id"])
    try:
        destination = store_get_destination(destination_id, user_id)
    except CatalogNotFound as exc:
        raise _not_found(exc) from exc
    try:
        deleted = default_destination_secret_store().delete(
            user_id=user_id,
            secret_ref=str(destination["secret_ref"]),
        )
    except DestinationSecretConfigurationError as exc:
        raise _secret_store_unavailable(exc) from exc
    return {"deleted": deleted, "secret_ref": destination["secret_ref"]}


@router.delete("/destinations/{destination_id}")
def delete_destination(
    destination_id: str, current_user: CurrentUser, _csrf: Csrf = None
) -> dict[str, Any]:
    try:
        store_delete_destination(destination_id, str(current_user["id"]))
        return {"deleted": destination_id}
    except CatalogNotFound as exc:
        raise _not_found(exc) from exc


@router.post("/destinations/{destination_id}/verify")
def verify_destination(
    destination_id: str, current_user: CurrentUser, _csrf: Csrf = None
) -> dict[str, Any]:
    try:
        return store_verify_destination(destination_id, str(current_user["id"]))
    except CatalogNotFound as exc:
        raise _not_found(exc) from exc
    except CatalogVerifyFailed as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/assets")
def create_asset(
    request: AssetCreate, current_user: CurrentUser, _csrf: Csrf = None
) -> dict[str, Any]:
    return store_create_asset(
        user_id=str(current_user["id"]),
        source_object_key=request.source_object_key,
    )


@router.get("/assets")
def list_assets(current_user: CurrentUser) -> dict[str, Any]:
    return {
        "assets": store_list_assets(str(current_user["id"])),
        "server_time": time.time(),
    }


@router.get("/assets/{asset_id}")
def get_asset(asset_id: str, current_user: CurrentUser) -> dict[str, Any]:
    try:
        return store_get_asset(asset_id, str(current_user["id"]))
    except CatalogNotFound as exc:
        raise _not_found(exc) from exc


@router.delete("/assets/{asset_id}")
def delete_asset(
    asset_id: str, current_user: CurrentUser, _csrf: Csrf = None
) -> dict[str, Any]:
    try:
        store_delete_asset(asset_id, str(current_user["id"]))
        return {"deleted": asset_id}
    except CatalogNotFound as exc:
        raise _not_found(exc) from exc
