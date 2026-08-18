"""FastAPI routes for the Destination / Asset catalog."""

from __future__ import annotations

import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth_api import require_csrf, require_user
from catalog_store import (
    CatalogNotFound,
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


class DestinationCreate(BaseModel):
    type: str = Field(default="rtmp", pattern="^(rtmp|rtmps|srt)$")
    display_name: str = Field(min_length=1, max_length=128)
    server_url: str = Field(min_length=1, max_length=500)
    secret_ref: str = Field(min_length=1, max_length=200)


class DestinationUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    server_url: str | None = Field(default=None, min_length=1, max_length=500)
    secret_ref: str | None = Field(default=None, min_length=1, max_length=200)


class AssetCreate(BaseModel):
    source_object_key: str = Field(min_length=1, max_length=500)


router = APIRouter(prefix="/v1")

CurrentUser = Annotated[dict[str, Any], Depends(require_user)]
Csrf = Annotated[None, Depends(require_csrf)]


def _not_found(exc: CatalogNotFound) -> HTTPException:
    return HTTPException(status_code=404, detail="unknown item")


@router.post("/destinations")
def create_destination(
    request: DestinationCreate, current_user: CurrentUser, _csrf: Csrf = None
) -> dict[str, Any]:
    return store_create_destination(
        user_id=str(current_user["id"]),
        type=request.type,
        display_name=request.display_name,
        server_url=request.server_url,
        secret_ref=request.secret_ref,
    )


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
            display_name=request.display_name,
            server_url=request.server_url,
            secret_ref=request.secret_ref,
        )
    except CatalogNotFound as exc:
        raise _not_found(exc) from exc


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
