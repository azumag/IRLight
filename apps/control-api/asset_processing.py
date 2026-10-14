"""Dependency-free asset processing helpers for Issue #7.

This module owns the *metadata* boundary for processing an uploaded standby asset
into distribution variants. It deliberately does not perform object storage I/O,
transcoding, or Node prefetch; those remain separately testable/replaceable.
"""

from __future__ import annotations

import binascii
import hashlib

from asset_upload_intent import (
    SUPPORTED_VARIANT_PROFILES,
    normalize_image_content_type,
)

# Keep the same Node-local standby bound so processing agrees with upload and
# decoder handoff.
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_IMAGE_PIXELS = 16 * 1024 * 1024
MAX_IMAGE_DIMENSION = 16_384
PROCESSING_STATUSES = ("UPLOADING", "PROCESSING", "READY", "FAILED", "DELETED")


class AssetProcessingError(ValueError):
    """Controlled fail-closed processing validation error."""


def normalize_processing_status(value: object) -> str:
    if not isinstance(value, str) or value not in PROCESSING_STATUSES:
        raise AssetProcessingError("unsupported asset processing status")
    return value


def _png_dimensions(payload: bytes) -> tuple[int, int] | None:
    if len(payload) < 33 or not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    if payload[8:12] != b"\x00\x00\x00\r" or payload[12:16] != b"IHDR":
        return None
    ihdr = payload[16:29]
    expected_crc = int.from_bytes(payload[29:33], "big")
    actual_crc = binascii.crc32(b"IHDR" + ihdr) & 0xFFFFFFFF
    if actual_crc != expected_crc:
        return None
    width = int.from_bytes(ihdr[0:4], "big")
    height = int.from_bytes(ihdr[4:8], "big")
    bit_depth = ihdr[8]
    color_type = ihdr[9]
    compression = ihdr[10]
    filter_method = ihdr[11]
    interlace = ihdr[12]
    allowed_depths = {
        0: {1, 2, 4, 8, 16},
        2: {8, 16},
        3: {1, 2, 4, 8},
        4: {8, 16},
        6: {8, 16},
    }
    if bit_depth not in allowed_depths.get(color_type, set()):
        return None
    if compression != 0 or filter_method != 0 or interlace not in {0, 1}:
        return None
    return width, height


_JPEG_SOF_MARKERS = frozenset(
    {
        0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB,
        0xCD, 0xCE, 0xCF,
    }
)


def _jpeg_dimensions(payload: bytes) -> tuple[int, int] | None:
    if len(payload) < 4 or not payload.startswith(b"\xff\xd8"):
        return None
    offset = 2
    while offset < len(payload):
        if payload[offset] != 0xFF:
            return None
        while offset < len(payload) and payload[offset] == 0xFF:
            offset += 1
        if offset >= len(payload):
            return None
        marker = payload[offset]
        offset += 1
        if marker == 0x00:
            return None
        if marker == 0xD8:
            continue
        if marker in {0xD9, 0xDA}:
            return None
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(payload):
            return None
        segment_length = int.from_bytes(payload[offset : offset + 2], "big")
        if segment_length < 2:
            return None
        segment_end = offset + segment_length
        if segment_end > len(payload):
            return None
        if marker in _JPEG_SOF_MARKERS:
            if segment_length < 11:
                return None
            precision = payload[offset + 2]
            height = int.from_bytes(payload[offset + 3 : offset + 5], "big")
            width = int.from_bytes(payload[offset + 5 : offset + 7], "big")
            component_count = payload[offset + 7]
            if precision <= 0 or precision > 16 or component_count <= 0:
                return None
            if segment_length != 8 + 3 * component_count:
                return None
            return width, height
        offset = segment_end
    return None


def _webp_dimensions(payload: bytes, *, file_size: int) -> tuple[int, int] | None:
    if len(payload) < 20 or payload[:4] != b"RIFF" or payload[8:12] != b"WEBP":
        return None
    riff_size = int.from_bytes(payload[4:8], "little")
    if riff_size + 8 != file_size:
        return None
    chunk_type = payload[12:16]
    chunk_size = int.from_bytes(payload[16:20], "little")
    padded_chunk_size = chunk_size + (chunk_size & 1)
    if 20 + padded_chunk_size > file_size:
        return None
    if chunk_type == b"VP8X":
        if chunk_size != 10 or len(payload) < 30:
            return None
        width = 1 + int.from_bytes(payload[24:27], "little")
        height = 1 + int.from_bytes(payload[27:30], "little")
        return width, height
    if chunk_type == b"VP8L":
        if chunk_size < 5 or len(payload) < 25 or payload[20] != 0x2F:
            return None
        b1, b2, b3, b4 = payload[21:25]
        width = 1 + b1 + ((b2 & 0x3F) << 8)
        height = 1 + ((b2 & 0xC0) >> 6) + (b3 << 2) + ((b4 & 0x0F) << 10)
        return width, height
    if chunk_type == b"VP8 ":
        if (
            chunk_size < 10
            or len(payload) < 30
            or payload[23:26] != b"\x9d\x01\x2a"
        ):
            return None
        width = int.from_bytes(payload[26:28], "little") & 0x3FFF
        height = int.from_bytes(payload[28:30], "little") & 0x3FFF
        return width, height
    return None


def image_dimensions(payload: bytes) -> tuple[int, int] | None:
    """Return validated dimensions for a supported MVP image, or None."""

    if len(payload) > MAX_IMAGE_BYTES:
        return None
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return _png_dimensions(payload)
    if payload.startswith(b"\xff\xd8"):
        return _jpeg_dimensions(payload)
    if payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return _webp_dimensions(payload, file_size=len(payload))
    return None


def normalize_image_blob(payload: object) -> dict[str, object]:
    """Validate magic bytes, dimensions, area bound, and basic MIME.

    This is the fast pre-decode boundary used before any transcode step. It does
    not claim that the file is fully decodable; full decode/decompression
    budget remains caller responsibility.
    """

    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise AssetProcessingError("image payload must be bytes")
    data = bytes(payload)
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise AssetProcessingError("image size is outside the allowed range")
    content_type = None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        content_type = "image/png"
    elif data.startswith(b"\xff\xd8"):
        content_type = "image/jpeg"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        content_type = "image/webp"
    if content_type is None:
        raise AssetProcessingError("unsupported standby asset content type")
    dimensions = image_dimensions(data)
    if dimensions is None:
        raise AssetProcessingError("could not determine image dimensions")
    width, height = dimensions
    if width <= 0 or height <= 0:
        raise AssetProcessingError("image dimensions must be positive")
    if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
        raise AssetProcessingError("image dimension exceeds the allowed limit")
    if width > MAX_IMAGE_PIXELS // height:
        raise AssetProcessingError("image pixel count exceeds the allowed limit")
    sha256 = hashlib.sha256(data).hexdigest()
    return {
        "content_type": content_type,
        "width": width,
        "height": height,
        "size_bytes": len(data),
        "sha256": sha256,
    }


def normalize_variant_profile_set(profiles: object) -> list[str]:
    if profiles is None:
        return list(SUPPORTED_VARIANT_PROFILES)
    if not isinstance(profiles, (list, tuple, set, frozenset)):
        raise AssetProcessingError("variant profiles must be a sequence")
    normalized = []
    for profile in profiles:
        from asset_upload_intent import normalize_variant_profile

        normalized.append(normalize_variant_profile(profile))
    # Preserve canonical order and de-duplicate.
    result = []
    for profile in SUPPORTED_VARIANT_PROFILES:
        if profile in normalized and profile not in result:
            result.append(profile)
    if not result:
        raise AssetProcessingError("at least one variant profile is required")
    return result


def build_variant_record(
    *,
    profile: object,
    object_key: object | None = None,
    sha256: object | None = None,
    size_bytes: object | None = None,
) -> dict[str, object]:
    from asset_upload_intent import normalize_variant_profile, variant_object_key

    normalized_profile = normalize_variant_profile(profile)
    if object_key is None:
        # Caller must ultimately bind the key via asset_upload_intent; leave it
        # unset rather than inventing one.
        object_key = None
    record: dict[str, object] = {"profile": normalized_profile}
    if object_key is not None:
        # Validate shape when supplied.
        if not isinstance(object_key, str) or not object_key:
            raise AssetProcessingError("variant object key must be a non-empty string")
        record["object_key"] = object_key
    if sha256 is not None:
        if (
            not isinstance(sha256, str)
            or len(sha256) != 64
            or any(c not in "0123456789abcdef" for c in sha256)
        ):
            raise AssetProcessingError("variant sha256 must be 64 lowercase hex characters")
        record["sha256"] = sha256
    if size_bytes is not None:
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes <= 0:
            raise AssetProcessingError("variant size_bytes must be a positive integer")
        record["size_bytes"] = size_bytes
    return record
