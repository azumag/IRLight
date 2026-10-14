"""Persistent catalog store for destinations and standby assets.

Pure-python store (no FastAPI dependency) so the CRUD logic is unit-testable
without the web framework. The API layer in catalog_api.py wraps this store.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from asset_upload_intent import (
    AssetUploadIntentError,
    MAX_UPLOAD_BYTES,
    is_owner_bound_source_object_key,
    normalize_image_content_type,
    source_object_key,
    validate_upload_completion,
)
from asset_processing import MAX_IMAGE_DIMENSION
from destination_probe import DestinationProbeError, probe_destination
from destination_url_safety import (
    DestinationUrlSafetyError,
    validate_destination_url_secret_safety,
)
from state_safety import load_json_authority, mark_initialized, was_initialized


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
            try:
                json.dump(
                    payload,
                    handle,
                    ensure_ascii=False,
                    sort_keys=True,
                    allow_nan=False,
                )
            except (TypeError, ValueError) as exc:
                raise CatalogStateError("catalog state cannot be encoded") from exc
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
            value = load_json_authority(handle)
    except FileNotFoundError:
        if was_initialized(path):
            raise CatalogStateError(
                f"catalog state {path} disappeared after initialization"
            )
        return default
    except (json.JSONDecodeError, ValueError, OSError) as exc:
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


def _require_catalog_string(
    item: dict[str, Any], field: str, *, context: str
) -> str:
    value = item.get(field)
    if not isinstance(value, str) or not value:
        raise CatalogStateError(f"{context} has invalid {field}")
    return value


def _validate_catalog_optional_bool(
    item: dict[str, Any], field: str, *, context: str
) -> bool | None:
    if field not in item:
        return None
    value = item[field]
    if not isinstance(value, bool):
        raise CatalogStateError(f"{context} has invalid {field}")
    return value


def _require_catalog_timestamp(
    item: dict[str, Any], field: str, *, context: str
) -> float:
    value = item.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CatalogStateError(f"{context} has invalid {field}")
    try:
        normalized = float(value)
    except (OverflowError, TypeError, ValueError):
        raise CatalogStateError(f"{context} has invalid {field}") from None
    if not math.isfinite(normalized) or normalized < 0:
        raise CatalogStateError(f"{context} has invalid {field}")
    return normalized


def _validated_catalog_now() -> float:
    return _require_catalog_timestamp(
        {"now": time.time()}, "now", context="catalog clock"
    )


def _require_catalog_optional_timestamp(
    item: dict[str, Any], field: str, *, context: str
) -> float | None:
    if field not in item:
        raise CatalogStateError(f"{context} has invalid {field}")
    if item[field] is None:
        return None
    return _require_catalog_timestamp(item, field, context=context)


def _validate_catalog_optional_string(
    item: dict[str, Any], field: str, *, context: str
) -> str | None:
    if field not in item:
        return None
    value = item[field]
    if value is not None and not isinstance(value, str):
        raise CatalogStateError(f"{context} has invalid {field}")
    return value


def _validate_catalog_optional_int(
    item: dict[str, Any],
    field: str,
    *,
    context: str,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int | None:
    if field not in item:
        return None
    value = item[field]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise CatalogStateError(f"{context} has invalid {field}")
    if minimum is not None and value < minimum:
        raise CatalogStateError(f"{context} has invalid {field}")
    if maximum is not None and value > maximum:
        raise CatalogStateError(f"{context} has invalid {field}")
    return value


def _validate_catalog_optional_sha256(
    item: dict[str, Any], field: str, *, context: str
) -> str | None:
    if field not in item:
        return None
    value = item[field]
    if value is None:
        return None
    if not isinstance(value, str) or len(value) != 64:
        raise CatalogStateError(f"{context} has invalid {field}")
    if any(char not in "0123456789abcdef" for char in value):
        raise CatalogStateError(f"{context} has invalid {field}")
    return value


def _validate_catalog_optional_timestamp_if_present(
    item: dict[str, Any], field: str, *, context: str
) -> float | None:
    if field not in item:
        return None
    if item[field] is None:
        return None
    return _require_catalog_timestamp(item, field, context=context)


def _validate_catalog_optional_dict(
    item: dict[str, Any], field: str, *, context: str
) -> dict[str, Any] | None:
    if field not in item:
        return None
    value = item[field]
    if value is not None and not isinstance(value, dict):
        raise CatalogStateError(f"{context} has invalid {field}")
    return value


def _validate_catalog_identity(
    item_id: str, item: dict[str, Any], *, context: str
) -> None:
    stored_id = _require_catalog_string(item, "id", context=context)
    _require_catalog_string(item, "user_id", context=context)
    if stored_id != item_id:
        raise CatalogStateError(f"{context} id does not match its key")


def _validate_catalog_authority(catalog: dict[str, Any]) -> dict[str, Any]:
    destinations = catalog.get("destinations")
    assets = catalog.get("assets")
    if not isinstance(destinations, dict) or not isinstance(assets, dict):
        raise CatalogStateError("catalog state has invalid structure")

    for destination_id, item in destinations.items():
        if (
            not isinstance(destination_id, str)
            or not destination_id
            or not isinstance(item, dict)
        ):
            raise CatalogStateError("catalog state has an invalid destination record")
        context = "catalog destination record"
        _validate_catalog_identity(destination_id, item, context=context)
        _require_catalog_string(item, "type", context=context)
        _require_catalog_string(item, "display_name", context=context)
        server_url = _require_catalog_string(item, "server_url", context=context)
        _require_catalog_string(item, "secret_ref", context=context)
        try:
            _validate_destination_server_url(server_url)
        except CatalogValidationError as exc:
            raise CatalogStateError(
                "catalog contains a destination URL with embedded credential material"
            ) from exc
        _require_catalog_timestamp(item, "created_at", context=context)
        _require_catalog_timestamp(item, "updated_at", context=context)
        # Older persisted destinations predate `enabled`, verification error,
        # and transport metadata. Validate those fields when present without
        # making historical records unreadable in the absence of a migration.
        _validate_catalog_optional_bool(item, "enabled", context=context)
        _require_catalog_string(item, "verification_status", context=context)
        _require_catalog_optional_timestamp(item, "last_verified_at", context=context)
        _validate_catalog_optional_string(
            item, "last_verification_error", context=context
        )
        _validate_catalog_optional_dict(item, "verification_transport", context=context)

    for asset_id, item in assets.items():
        if not isinstance(asset_id, str) or not asset_id or not isinstance(item, dict):
            raise CatalogStateError("catalog state has an invalid asset record")
        context = "catalog asset record"
        _validate_catalog_identity(asset_id, item, context=context)
        _require_catalog_string(item, "source_object_key", context=context)
        _require_catalog_string(item, "processing_status", context=context)
        _require_catalog_timestamp(item, "created_at", context=context)
        _require_catalog_timestamp(item, "updated_at", context=context)
        # Upload-intent metadata (optional for historical records).
        _validate_catalog_optional_string(item, "content_type", context=context)
        _validate_catalog_optional_int(
            item, "max_bytes", context=context, minimum=1, maximum=MAX_UPLOAD_BYTES
        )
        _validate_catalog_optional_int(
            item, "uploaded_size_bytes", context=context, minimum=1, maximum=MAX_UPLOAD_BYTES
        )
        _validate_catalog_optional_sha256(item, "uploaded_sha256", context=context)
        _validate_catalog_optional_sha256(item, "processed_sha256", context=context)
        _validate_catalog_optional_int(
            item, "processed_width", context=context, minimum=1, maximum=MAX_IMAGE_DIMENSION
        )
        _validate_catalog_optional_int(
            item, "processed_height", context=context, minimum=1, maximum=MAX_IMAGE_DIMENSION
        )
        _validate_catalog_optional_string(item, "failure_reason", context=context)
        _validate_catalog_optional_dict(item, "variants", context=context)
        _validate_catalog_optional_timestamp_if_present(item, "ready_at", context=context)

    return catalog


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
    return _validate_catalog_authority(read_json(CATALOG_PATH, _default_catalog()))


def _save(catalog: dict[str, Any]) -> None:
    # Never publish a payload that the reader would reject on the next request.
    _validate_catalog_authority(catalog)
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
        now = _validated_catalog_now()
        item_id = str(uuid.uuid4())
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
        now = _validated_catalog_now()
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
        item["updated_at"] = now
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
        now = _validated_catalog_now()
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
        now = _validated_catalog_now()
        item["verification_status"] = "FAILED"
        item["last_verification_error"] = reason[:200]
        item["verification_transport"] = None
        item["updated_at"] = now
        catalog["destinations"][destination_id] = item
        _save(catalog)


def create_asset(*, user_id: str, source_object_key: str) -> dict[str, Any]:
    with _catalog_lock(exclusive=True):
        catalog = _load()
        now = _validated_catalog_now()
        asset_id = str(uuid.uuid4())
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

def complete_asset_processing(
    *,
    asset_id: str,
    user_id: str,
    processed_sha256: object | None = None,
    processed_width: object | None = None,
    processed_height: object | None = None,
    variants: object | None = None,
    failure_reason: object | None = None,
) -> dict[str, Any]:
    """Move a ``PROCESSING`` asset to ``READY`` (or ``FAILED``).

    The asset must currently be ``PROCESSING``.  ``failure_reason`` (when
    supplied) marks the asset ``FAILED`` instead of ``READY``.  Supplied
    variants must be a mapping of supported variant profile -> metadata dict.
    """

    from asset_processing import AssetProcessingError
    from asset_upload_intent import normalize_variant_profile

    try:
        if failure_reason is not None:
            if not isinstance(failure_reason, str) or not failure_reason.strip():
                raise CatalogValidationError("failure reason must be a non-empty string")
        if processed_sha256 is not None and (
            not isinstance(processed_sha256, str)
            or len(processed_sha256) != 64
            or any(c not in "0123456789abcdef" for c in processed_sha256)
        ):
            raise CatalogValidationError("processed sha256 must be 64 lowercase hex characters")
        if processed_width is not None:
            if (
                isinstance(processed_width, bool)
                or not isinstance(processed_width, int)
                or processed_width < 1
                or processed_width > MAX_IMAGE_DIMENSION
            ):
                raise CatalogValidationError("processed width is invalid")
        if processed_height is not None:
            if (
                isinstance(processed_height, bool)
                or not isinstance(processed_height, int)
                or processed_height < 1
                or processed_height > MAX_IMAGE_DIMENSION
            ):
                raise CatalogValidationError("processed height is invalid")

        with _catalog_lock(exclusive=True):
            catalog = _load()
            asset = _get_owned(catalog, "assets", asset_id, user_id)
            if asset.get("processing_status") != "PROCESSING":
                raise CatalogValidationError("asset is not processing")
            now = _validated_catalog_now()
            if failure_reason is not None:
                asset["processing_status"] = "FAILED"
                asset["failure_reason"] = failure_reason.strip()[:500]
                asset["updated_at"] = now
                _save(catalog)
                return dict(asset)

            if processed_sha256 is None:
                processed_sha256 = asset.get("uploaded_sha256")

            from asset_upload_intent import variant_object_key

            normalized_variants: dict[str, Any] | None = None
            if variants is not None:
                if not isinstance(variants, dict) or not variants:
                    raise CatalogValidationError("variants must be a non-empty mapping")
                normalized_variants = {}
                for profile, record in variants.items():
                    normalized_profile = normalize_variant_profile(profile)
                    if not isinstance(record, dict):
                        raise CatalogValidationError("variant record must be an object")
                    # Server-derived object key: callers may only repeat the
                    # exact key we would derive; any other value is fail-closed.
                    derived_key = variant_object_key(
                        user_id=user_id,
                        asset_id=asset_id,
                        profile=normalized_profile,
                    )
                    supplied_key = record.get("object_key", None)
                    if supplied_key is not None and supplied_key != derived_key:
                        raise CatalogValidationError(
                            "variant object key is not bound to this owner and asset"
                        )
                    # Type/range-check any other record fields.
                    if "sha256" in record and record["sha256"] is not None:
                        sha = record["sha256"]
                        if (
                            not isinstance(sha, str)
                            or len(sha) != 64
                            or any(c not in "0123456789abcdef" for c in sha)
                        ):
                            raise CatalogValidationError(
                                "variant sha256 must be 64 lowercase hex characters"
                            )
                    if "size_bytes" in record and record["size_bytes"] is not None:
                        size = record["size_bytes"]
                        if isinstance(size, bool) or not isinstance(size, int):
                            raise CatalogValidationError(
                                "variant size_bytes must be a positive integer"
                            )
                        if size <= 0 or size > MAX_UPLOAD_BYTES:
                            raise CatalogValidationError(
                                "variant size_bytes is outside the allowed range"
                            )
                    normalized_record = dict(record)
                    # Always store the server-derived key so caller intent cannot
                    # address another owner's namespace.
                    normalized_record["object_key"] = derived_key
                    normalized_variants[normalized_profile] = normalized_record

            asset["processing_status"] = "READY"
            asset["ready_at"] = now
            if processed_sha256 is not None:
                asset["processed_sha256"] = processed_sha256
            if processed_width is not None:
                asset["processed_width"] = processed_width
            if processed_height is not None:
                asset["processed_height"] = processed_height
            if normalized_variants is not None:
                asset["variants"] = normalized_variants
            asset["updated_at"] = now
            _save(catalog)
            return dict(asset)
    except (AssetUploadIntentError, AssetProcessingError) as exc:
        raise CatalogValidationError(str(exc)) from exc


def fail_asset_processing(
    *, asset_id: str, user_id: str, reason: str
) -> dict[str, Any]:
    return complete_asset_processing(
        asset_id=asset_id, user_id=user_id, failure_reason=reason
    )


mark_asset_ready = complete_asset_processing
process_asset = complete_asset_processing
mark_asset_failed = fail_asset_processing
complete_processing = complete_asset_processing


def delete_asset(asset_id: str, user_id: str) -> None:
    with _catalog_lock(exclusive=True):
        catalog = _load()
        _get_owned(catalog, "assets", asset_id, user_id)
        del catalog["assets"][asset_id]
        _save(catalog)


def create_asset_upload_intent(*, user_id: str, content_type: str) -> dict[str, Any]:
    """Issue a server-generated, owner-bound object key for a new upload.

    The caller never chooses the object key: it is derived from the validated
    owner id and the server-issued asset id by ``asset_upload_intent``. The
    asset starts in ``UPLOADING`` and only ``complete_asset_upload`` can move it
    into ``PROCESSING``. The legacy metadata-only ``create_asset`` path remains
    for compatibility but grants no write access to any object key.
    """

    try:
        normalized_type = normalize_image_content_type(content_type)
        with _catalog_lock(exclusive=True):
            catalog = _load()
            now = _validated_catalog_now()
            asset_id = str(uuid.uuid4())
            key = source_object_key(user_id=user_id, asset_id=asset_id)
            item = {
                "id": asset_id,
                "user_id": user_id,
                "source_object_key": key,
                "processing_status": "UPLOADING",
                "content_type": normalized_type,
                "max_bytes": MAX_UPLOAD_BYTES,
                "created_at": now,
                "updated_at": now,
            }
            catalog["assets"][asset_id] = item
            _save(catalog)
            return dict(item)
    except AssetUploadIntentError as exc:
        raise CatalogValidationError(str(exc)) from exc


def complete_asset_upload(
    *,
    asset_id: str,
    user_id: str,
    content_type: str,
    object_key: str,
    size_bytes: int,
    sha256: str,
) -> dict[str, Any]:
    """Validate an upload completion and move the asset into ``PROCESSING``.

    Fail closed: a completion whose content type, object key, size, or checksum
    does not match the issued intent never advances. The asset must currently be
    ``UPLOADING`` so replays and out-of-order completions are rejected instead
    of silently re-arming a finished asset. Only the owning user can complete
    the upload, and only the exact server-derived key is accepted.
    """

    try:
        normalized_type = normalize_image_content_type(content_type)
        with _catalog_lock(exclusive=True):
            catalog = _load()
            asset = _get_owned(catalog, "assets", asset_id, user_id)
            if asset.get("processing_status") != "UPLOADING":
                raise CatalogValidationError("asset is not awaiting an upload")
            if asset.get("content_type") != normalized_type:
                raise CatalogValidationError(
                    "upload completion content type does not match the issued intent"
                )
            completion = validate_upload_completion(
                user_id=user_id,
                asset_id=asset_id,
                content_type=normalized_type,
                object_key=object_key,
                size_bytes=size_bytes,
                sha256=sha256,
            )
            if not is_owner_bound_source_object_key(
                user_id=user_id,
                asset_id=asset_id,
                object_key=asset.get("source_object_key"),
            ):
                raise CatalogValidationError(
                    "stored asset object key is not bound to this owner and asset"
                )
            now = _validated_catalog_now()
            asset["processing_status"] = "PROCESSING"
            asset["uploaded_size_bytes"] = completion["size_bytes"]
            asset["uploaded_sha256"] = completion["sha256"]
            asset["updated_at"] = now
            _save(catalog)
            return dict(asset)
    except AssetUploadIntentError as exc:
        raise CatalogValidationError(str(exc)) from exc
