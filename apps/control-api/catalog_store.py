"""Persistent catalog store for destinations and standby assets.

Pure-python store (no FastAPI dependency) so the CRUD logic is unit-testable
without the web framework. The API layer in catalog_api.py wraps this store.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from destination_probe import DestinationProbeError, probe_destination


STATE_DIR = Path(os.getenv("STATE_DIR", "/state"))
CATALOG_PATH = STATE_DIR / "catalog.json"
LOCK = threading.Lock()


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


def _default_catalog() -> dict[str, Any]:
    return {"destinations": {}, "assets": {}}


def ensure_catalog() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not CATALOG_PATH.exists():
        atomic_write_json(CATALOG_PATH, _default_catalog())


def _load() -> dict[str, Any]:
    return read_json(CATALOG_PATH, _default_catalog())


def _save(catalog: dict[str, Any]) -> None:
    atomic_write_json(CATALOG_PATH, catalog)


class CatalogNotFound(KeyError):
    pass


class CatalogVerifyFailed(ValueError):
    pass


def _get_owned(catalog: dict[str, Any], kind: str, item_id: str, user_id: str) -> dict[str, Any]:
    items = catalog.get(kind, {})
    item = items.get(item_id)
    if item is None or item.get("user_id") != user_id:
        raise CatalogNotFound(item_id)
    return item


def create_destination(
    *,
    user_id: str,
    type: str,
    display_name: str,
    server_url: str,
    secret_ref: str,
) -> dict[str, Any]:
    with LOCK:
        catalog = _load()
        item_id = str(uuid.uuid4())
        now = time.time()
        item = {
            "id": item_id,
            "user_id": user_id,
            "type": type,
            "display_name": display_name,
            "server_url": server_url,
            "secret_ref": secret_ref,
            "enabled": True,
            "verification_status": "UNVERIFIED",
            "last_verified_at": None,
            "last_verification_error": None,
            "verification_transport": None,
            "created_at": now,
            "updated_at": now,
        }
        catalog["destinations"][item_id] = item
        _save(catalog)
        return dict(item)


def list_destinations(user_id: str) -> list[dict[str, Any]]:
    catalog = _load()
    return [
        item
        for item in catalog.get("destinations", {}).values()
        if item.get("user_id") == user_id
    ]


def get_destination(destination_id: str, user_id: str) -> dict[str, Any]:
    with LOCK:
        catalog = _load()
        return dict(_get_owned(catalog, "destinations", destination_id, user_id))


def update_destination(
    destination_id: str,
    *,
    user_id: str,
    display_name: str | None = None,
    server_url: str | None = None,
    secret_ref: str | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    with LOCK:
        catalog = _load()
        item = _get_owned(catalog, "destinations", destination_id, user_id)
        if display_name is not None:
            item["display_name"] = display_name
        if server_url is not None:
            if server_url != item.get("server_url"):
                item["verification_status"] = "UNVERIFIED"
                item["last_verified_at"] = None
                item["last_verification_error"] = None
                item["verification_transport"] = None
            item["server_url"] = server_url
        if secret_ref is not None:
            item["secret_ref"] = secret_ref
        if enabled is not None:
            item["enabled"] = enabled
        item["updated_at"] = time.time()
        catalog["destinations"][destination_id] = item
        _save(catalog)
        return dict(item)


def delete_destination(destination_id: str, user_id: str) -> None:
    with LOCK:
        catalog = _load()
        _get_owned(catalog, "destinations", destination_id, user_id)
        del catalog["destinations"][destination_id]
        _save(catalog)


def verify_destination(
    destination_id: str,
    user_id: str,
    *,
    probe: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Probe the configured transport and persist the verification result.

    The network operation intentionally runs outside ``LOCK`` so a slow remote
    destination cannot block unrelated catalog reads and writes. Before writing
    the result back, the URL is checked again; a concurrent edit therefore
    cannot stamp a result for an old URL onto a newer destination.
    """
    verifier = probe or probe_destination
    with LOCK:
        catalog = _load()
        item = dict(_get_owned(catalog, "destinations", destination_id, user_id))
        url = str(item.get("server_url", ""))

    try:
        result = verifier(url)
    except DestinationProbeError as exc:
        _record_verification_failure(destination_id, user_id, url, str(exc))
        raise CatalogVerifyFailed(str(exc)) from exc
    except Exception as exc:
        _record_verification_failure(
            destination_id, user_id, url, "destination verification failed"
        )
        raise CatalogVerifyFailed("destination verification failed") from exc

    with LOCK:
        catalog = _load()
        current = _get_owned(catalog, "destinations", destination_id, user_id)
        if current.get("server_url") != url:
            raise CatalogVerifyFailed("destination changed during verification")
        now = time.time()
        current["verification_status"] = "VERIFIED"
        current["last_verified_at"] = now
        current["last_verification_error"] = None
        current["verification_transport"] = {
            "protocol": result.get("protocol"),
            "peer_ip": result.get("peer_ip"),
            "peer_port": result.get("peer_port"),
            "elapsed_ms": result.get("elapsed_ms"),
        }
        current["updated_at"] = now
        catalog["destinations"][destination_id] = current
        _save(catalog)
        return dict(current)


def _record_verification_failure(
    destination_id: str,
    user_id: str,
    expected_url: str,
    reason: str,
) -> None:
    with LOCK:
        catalog = _load()
        item = _get_owned(catalog, "destinations", destination_id, user_id)
        if item.get("server_url") != expected_url:
            return
        item["verification_status"] = "FAILED"
        item["last_verification_error"] = reason[:200]
        item["verification_transport"] = None
        item["updated_at"] = time.time()
        catalog["destinations"][destination_id] = item
        _save(catalog)


def create_asset(*, user_id: str, source_object_key: str) -> dict[str, Any]:
    with LOCK:
        catalog = _load()
        asset_id = str(uuid.uuid4())
        now = time.time()
        item = {
            "id": asset_id,
            "user_id": user_id,
            "source_object_key": source_object_key,
            "processing_status": "PENDING",
            "created_at": now,
            "updated_at": now,
        }
        catalog["assets"][asset_id] = item
        _save(catalog)
        return dict(item)


def list_assets(user_id: str) -> list[dict[str, Any]]:
    catalog = _load()
    return [
        item
        for item in catalog.get("assets", {}).values()
        if item.get("user_id") == user_id
    ]


def get_asset(asset_id: str, user_id: str) -> dict[str, Any]:
    with LOCK:
        catalog = _load()
        return dict(_get_owned(catalog, "assets", asset_id, user_id))


def delete_asset(asset_id: str, user_id: str) -> None:
    with LOCK:
        catalog = _load()
        _get_owned(catalog, "assets", asset_id, user_id)
        del catalog["assets"][asset_id]
        _save(catalog)
