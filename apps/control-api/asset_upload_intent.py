"""Server-side authority for standby asset upload object keys.

The upload object key is never taken from user input. It is derived only from a
validated owner id and the server-issued asset id (plus a supported variant
profile for generated material), so a caller cannot address another owner's
namespace with a crafted key. This module is intentionally dependency-free so
the Control Plane API, the processing worker, and the Media Node prefetch
contract can share one authority and be unit tested without external services.

Object layout (see ``docs/standby-asset-upload-intent.md``)::

    users/{userId}/assets/{assetId}/source
    users/{userId}/assets/{assetId}/variants/720p30.mp4
    users/{userId}/assets/{assetId}/variants/1080p30.mp4
"""

from __future__ import annotations

import hashlib

ASSET_NAMESPACE = "users"
SOURCE_OBJECT_NAME = "source"
VARIANTS_PREFIX = "variants"
VARIANTS_PATH_SUFFIX = ".mp4"

# MVP image upload formats. SVG is intentionally excluded: it can carry script
# and external references and must never be served as a standby asset.
SUPPORTED_IMAGE_CONTENT_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}

SUPPORTED_VARIANT_PROFILES = ("720p30", "1080p30")

# Matches the Node-local standby bound so the same 32 MiB ceiling governs the
# object store, the processing worker, and the decoder handoff.
MAX_UPLOAD_BYTES = 32 * 1024 * 1024

_MAX_SEGMENT_LENGTH = 200
_MAX_CONTENT_TYPE_LENGTH = 200
_SHA256_HEX_LENGTH = hashlib.sha256().digest_size * 2
_HEX_DIGITS = frozenset("0123456789abcdef")


class AssetUploadIntentError(ValueError):
    """Controlled fail-closed object-key or upload-completion error."""


def _require_segment(value: object, *, field: str) -> str:
    """Return a validated single path segment (owner id or asset id).

    Only printable ASCII without path separators or dots-only values are
    accepted, so a segment can never widen, escape, or alias the namespace the
    key is derived from.
    """

    if not isinstance(value, str) or not value:
        raise AssetUploadIntentError(f"{field} must be a non-empty string")
    if len(value) > _MAX_SEGMENT_LENGTH:
        raise AssetUploadIntentError(f"{field} is too long")
    if value in {".", ".."} or value.startswith(".") or value.endswith("."):
        raise AssetUploadIntentError(f"{field} is not a valid path segment")
    for char in value:
        if not 0x21 <= ord(char) <= 0x7E:
            raise AssetUploadIntentError(f"{field} contains a disallowed character")
        if char in {"/", "\\"}:
            raise AssetUploadIntentError(f"{field} contains a path separator")
    return value


def normalize_image_content_type(content_type: object) -> str:
    """Return the canonical MVP image media type or fail closed.

    Parameters (``image/png; charset=binary``) are stripped before matching; the
    match is exact and case-insensitive on the base type only.
    """

    if not isinstance(content_type, str) or not content_type:
        raise AssetUploadIntentError("content type must be a non-empty string")
    if len(content_type) > _MAX_CONTENT_TYPE_LENGTH:
        raise AssetUploadIntentError("content type is too long")
    base = content_type.split(";", 1)[0].strip().lower()
    if base not in SUPPORTED_IMAGE_CONTENT_TYPES:
        raise AssetUploadIntentError("unsupported standby asset content type")
    return base


def normalize_variant_profile(profile: object) -> str:
    if profile not in SUPPORTED_VARIANT_PROFILES:
        raise AssetUploadIntentError("unsupported standby variant profile")
    return profile


def source_object_key(*, user_id: object, asset_id: object) -> str:
    """Derive the canonical server-side source key for one owned asset."""

    owner = _require_segment(user_id, field="user id")
    asset = _require_segment(asset_id, field="asset id")
    return f"{ASSET_NAMESPACE}/{owner}/assets/{asset}/{SOURCE_OBJECT_NAME}"


def variant_object_key(*, user_id: object, asset_id: object, profile: object) -> str:
    """Derive the canonical key for one generated standby variant."""

    owner = _require_segment(user_id, field="user id")
    asset = _require_segment(asset_id, field="asset id")
    normalized = normalize_variant_profile(profile)
    return (
        f"{ASSET_NAMESPACE}/{owner}/assets/{asset}/"
        f"{VARIANTS_PREFIX}/{normalized}{VARIANTS_PATH_SUFFIX}"
    )


def is_owner_bound_source_object_key(
    *, user_id: object, asset_id: object, object_key: object
) -> bool:
    """Return True only for the exact server-derived source key.

    Any malformed owner/asset id, empty key, foreign namespace, or prefix/suffix
    alias resolves to False rather than raising, so callers can use this as an
    admission predicate that never accepts an unbound key.
    """

    if not isinstance(object_key, str) or not object_key:
        return False
    try:
        expected = source_object_key(user_id=user_id, asset_id=asset_id)
    except AssetUploadIntentError:
        return False
    return object_key == expected


def _validated_sha256(value: object, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != _SHA256_HEX_LENGTH
        or any(char not in _HEX_DIGITS for char in value)
    ):
        raise AssetUploadIntentError(f"{field} must be 64 lowercase hex characters")
    return value


def _validated_size_bytes(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AssetUploadIntentError("uploaded object size must be an integer")
    if value <= 0 or value > MAX_UPLOAD_BYTES:
        raise AssetUploadIntentError("uploaded object size is outside the allowed range")
    return value


def validate_upload_completion(
    *,
    user_id: object,
    asset_id: object,
    content_type: object,
    object_key: object,
    size_bytes: object,
    sha256: object,
) -> dict[str, object]:
    """Validate an upload completion against the issued intent, or fail closed.

    Returns the normalized completion record. The object key must be exactly the
    owner-bound source key, the media type must be a supported image type, the
    size must be a positive integer within ``MAX_UPLOAD_BYTES``, and the digest
    must be 64 lowercase hex characters. Nothing here trusts a caller-supplied
    namespace or an unbounded size/hash string.
    """

    normalized_type = normalize_image_content_type(content_type)
    expected_key = source_object_key(user_id=user_id, asset_id=asset_id)
    if not isinstance(object_key, str) or object_key != expected_key:
        raise AssetUploadIntentError(
            "uploaded object key is not bound to this owner and asset"
        )
    return {
        "object_key": expected_key,
        "content_type": normalized_type,
        "size_bytes": _validated_size_bytes(size_bytes),
        "sha256": _validated_sha256(sha256, field="sha256"),
    }
