"""Persistent catalog store for destinations and standby assets.

Pure-python store (no FastAPI dependency) so the CRUD logic is unit-testable
without the web framework. The API layer in catalog_api.py wraps this store.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from destination_probe import DestinationProbeError, probe_destination
from destination_url_safety import (
    DestinationUrlSafetyError,
    validate_destination_url_secret_safety,
)
from state_safety import mark_initialized, was_initialized


STATE_DIR = Path(os.getenv("STATE_DIR", "/state"))
CATALOG_PATH = STATE_DIR / "catalog.json"
CATALOG_LOCK_PATH = STATE_DIR / ".catalog.lock"
LOCK = threading.RLock()


class CatalogStateError(RuntimeError):
    pass


class CatalogValidationError(ValueError):
    pass


@contextmanager
def _catalog_lock(*, exclusive: bool):
    with LOCK:
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            with CATALOG_LOCK_PATH.open("a+", encoding="utf-8") as handle:
                operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
                fcntl.flock(handle.fileno(), operation)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except CatalogStateError:
            raise
        except OSError as exc:
            raise CatalogStateError("catalog state cannot be locked") from exc


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        # Arm the initialization fuse before publishing authoritative state.
        # If this commit is interrupted, the next process fails closed rather
        # than treating a once-written catalog as a brand-new empty catalog.
        mark_initialized(path)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError:
        if was_initialized(path):
            raise CatalogStateError(
                f"catalog state {path} disappeared after initialization"
            )
        return default
    except (json.JSONDecodeError, OSError) as exc:
        raise CatalogStateError(f"catalog state {path} cannot be read") from exc
    if not isinstance(value, dict):
        raise CatalogStateError(f"catalog state {path} has invalid structure")
    return value


def _default_catalog() -> dict[str, Any]:
    return {"destinations": {}, "assets": {}}


def _validate_destination_server_url(server_url: str) -> None:
    try:
        validate_destination_url_secret_safety(server_url)
    except DestinationUrlSafetyError as exc:
        raise CatalogValidationError(str(exc)) from exc


def ensure_catalog() -> None:
    with _catalog_lock(exclusive=True):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        if not CATALOG_PATH.exists():
            if was_initialized(CATALOG_PATH):
                raise CatalogStateError("catalog state disappeared after initialization")
            atomic_write_json(CATALOG_PATH, _default_catalog())
        else:
            _load()
            mark_initialized(CATALOG_PATH)


def _load() -> dict[str, Any]:
    catalog = read_json(CATALOG_PATH, _default_catalog())
    if not isinstance(catalog.get("destinations"), dict) or not isinstance(
        catalog.get("assets"), dict
    ):
        raise CatalogStateError("catalog state has invalid structure")
    for section in ("destinations", "assets"):
        if any(
            not isinstance(key, str) or not isinstance(item, dict)
            for key, item in catalog[section].items()
        ):
            raise CatalogStateError("catalog state has an invalid record")

    # A credential-bearing URL is unsafe even for a read: list/get responses
    # would otherwise echo the persisted secret back to the API client. Fail
    # closed without rewriting the authority so an operator can recover it
    # deliberately and rotate the affected credential.
    for item in catalog["destinations"].values():
        server_url = item.get("server_url")
        if not isinstance(server_url, str):
            continue
        try:
            _validate_destination_server_url(server_url)
        except CatalogValidationError as exc:
            raise CatalogStateError(
                "catalog contains a destination URL with embedded credential material"
            ) from exc
    return catalog


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
    _validate_destination_server_url(server_url)
    with _catalog_lock(exclusive=True):
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
    with _catalog_lock(exclusive=False):
        catalog = _load()
        return [
            dict(item)
            for item in catalog["destinations"].values()
            if item.get("user_id") == user_id
        ]


def get_destination(destination_id: str, user_id: str) -> dict[str, Any]:
    with _catalog_lock(exclusive=False):
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
    if server_url is not None:
        _validate_destination_server_url(server_url)
    with _catalog_lock(exclusive=True):
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
    with _catalog_lock(exclusive=True):
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
    with _catalog_lock(exclusive=False):
        catalog = _load()
        item = dict(_get_owned(catalog, "destinations", destination_id, user_id))
        url = str(item.get("server_url", ""))
        destination_type = str(item.get("type", "")).lower()

    url_scheme = urlsplit(url).scheme.lower()
    if url_scheme != destination_type:
        reason = "destination type does not match server URL scheme"
        _record_verification_failure(destination_id, user_id, url, reason)
        raise CatalogVerifyFailed(reason)

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

    if str(result.get("protocol", "")).lower() != destination_type:
        reason = "destination probe protocol does not match destination type"
        _record_verification_failure(destination_id, user_id, url, reason)
        raise CatalogVerifyFailed(reason)

    with _catalog_lock(exclusive=True):
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
    with _catalog_lock(exclusive=True):
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
    with _catalog_lock(exclusive=True):
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
    with _catalog_lock(exclusive=False):
        catalog = _load()
        return [
            dict(item)
            for item in catalog["assets"].values()
            if item.get("user_id") == user_id
        ]


def get_asset(asset_id: str, user_id: str) -> dict[str, Any]:
    with _catalog_lock(exclusive=False):
        catalog = _load()
        return dict(_get_owned(catalog, "assets", asset_id, user_id))


def delete_asset(asset_id: str, user_id: str) -> None:
    with _catalog_lock(exclusive=True):
        catalog = _load()
        _get_owned(catalog, "assets", asset_id, user_id)
        del catalog["assets"][asset_id]
        _save(catalog)
