from __future__ import annotations

import hashlib
import os
import stat

from standby_asset import (
    MAX_IMAGE_BYTES,
    NODE_DEFAULT_IMAGE_PATH,
    StandbyAssetSelection,
    resolve_standby_asset,
)


_READ_CHUNK_BYTES = 64 * 1024
_SHA256_HEX_LENGTH = hashlib.sha256().digest_size * 2
_MAX_EXPECTED_SIZE_DIGITS = len(str(MAX_IMAGE_BYTES))


class StandbyIntegrityError(RuntimeError):
    """Controlled fail-closed checksum configuration or verification error."""


def _validated_expected_sha256(value: str | None) -> str | None:
    if value is None:
        return None
    if (
        len(value) != _SHA256_HEX_LENGTH
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise StandbyIntegrityError(
            "expected standby sha256 must be 64 lowercase hex characters"
        )
    return value


def _validated_expected_size_bytes(value: str | None) -> int | None:
    if value is None:
        return None
    if (
        not value
        or len(value) > _MAX_EXPECTED_SIZE_DIGITS
        or not value.isascii()
        or not value.isdigit()
    ):
        raise StandbyIntegrityError("expected standby size must be a bounded decimal integer")
    parsed = int(value, 10)
    if parsed <= 0 or parsed > MAX_IMAGE_BYTES:
        raise StandbyIntegrityError("expected standby size is outside the allowed range")
    return parsed


def _stat_signature(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def verify_selected_snapshot(
    selection: StandbyAssetSelection,
    *,
    expected_sha256: str | None,
    expected_size_bytes: str | None,
) -> None:
    """Verify the immutable decoder snapshot without reopening the source path.

    The Continuity selector already copied the source into a private unlinked
    snapshot. Integrity verification deliberately hashes that exact pinned fd so
    a pathname replacement between checksum verification and decoder selection
    cannot swap in different bytes.
    """

    expected_digest = _validated_expected_sha256(expected_sha256)
    expected_size = _validated_expected_size_bytes(expected_size_bytes)

    if expected_digest is None:
        if expected_size is None:
            return
        raise StandbyIntegrityError(
            "expected standby size cannot be used without an expected sha256"
        )

    fd = selection._pinned_fd
    identity = selection._pinned_identity
    if fd is None or identity is None:
        raise StandbyIntegrityError("standby snapshot is unavailable for verification")

    pread = getattr(os, "pread", None)
    if pread is None:
        raise StandbyIntegrityError("standby snapshot verification is unavailable")

    try:
        opened = os.fstat(fd)
    except OSError as exc:
        raise StandbyIntegrityError("standby snapshot is unavailable") from exc
    if not stat.S_ISREG(opened.st_mode):
        raise StandbyIntegrityError("standby snapshot is not a regular file")
    if (opened.st_dev, opened.st_ino) != identity:
        raise StandbyIntegrityError("standby snapshot identity changed")
    if opened.st_size <= 0 or opened.st_size > MAX_IMAGE_BYTES:
        raise StandbyIntegrityError("standby snapshot size is outside the allowed range")
    if expected_size is not None and opened.st_size != expected_size:
        raise StandbyIntegrityError("standby snapshot size does not match expected metadata")

    signature = _stat_signature(opened)
    digest = hashlib.sha256()
    offset = 0
    while offset < opened.st_size:
        try:
            chunk = pread(fd, min(_READ_CHUNK_BYTES, opened.st_size - offset), offset)
        except OSError as exc:
            raise StandbyIntegrityError("standby snapshot cannot be read safely") from exc
        if not chunk:
            raise StandbyIntegrityError("standby snapshot changed during verification")
        digest.update(chunk)
        offset += len(chunk)

    try:
        after = os.fstat(fd)
    except OSError as exc:
        raise StandbyIntegrityError("standby snapshot became unavailable") from exc
    if _stat_signature(after) != signature:
        raise StandbyIntegrityError("standby snapshot changed during verification")
    if digest.hexdigest() != expected_digest:
        raise StandbyIntegrityError("standby snapshot sha256 does not match expected metadata")


def resolve_integrity_checked_standby_asset(
    custom_path: str | None,
    fallback_path: str | None = NODE_DEFAULT_IMAGE_PATH,
    *,
    expected_sha256: str | None = None,
    expected_size_bytes: str | None = None,
) -> StandbyAssetSelection:
    """Resolve the standby image and optionally bind custom bytes to metadata.

    Checksum metadata is intentionally optional for compatibility with the
    existing trusted Node-local handoff. When either integrity setting is
    supplied, however, a custom image must pass the complete SHA-256 check or it
    is discarded and normal Node-default/synthetic fallback is used.
    """

    selection = resolve_standby_asset(custom_path, fallback_path)
    if selection.source != "CUSTOM":
        return selection
    if expected_sha256 is None and expected_size_bytes is None:
        return selection

    try:
        verify_selected_snapshot(
            selection,
            expected_sha256=expected_sha256,
            expected_size_bytes=expected_size_bytes,
        )
        return selection
    except StandbyIntegrityError:
        selection.close()

    fallback = resolve_standby_asset(None, fallback_path)
    fallback.custom_configured = True
    if fallback.source == "NODE_DEFAULT":
        fallback.fallback_reason = "ASSET_INTEGRITY_CHECK_FAILED"
    else:
        fallback.fallback_reason = (
            "ASSET_INTEGRITY_CHECK_FAILED_AND_NODE_DEFAULT_UNAVAILABLE"
        )
    return fallback
