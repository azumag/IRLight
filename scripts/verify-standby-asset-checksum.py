#!/usr/bin/env python3
"""Inspect or verify one Node-local standby asset checksum safely.

This helper is intentionally read-only. It produces a small canonical JSON
record that a future asset-processing/prefetch workflow can persist alongside
its own ownership/version metadata. It does not fetch remote objects, mutate
asset state, or make an asset READY by itself.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any


MAX_IMAGE_BYTES = 32 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024
_SHA256_HEX_LENGTH = hashlib.sha256().digest_size * 2


class AssetChecksumError(RuntimeError):
    """Controlled fail-closed input/verification error."""


def _stat_signature(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _validate_expected_sha256(value: str | None) -> str | None:
    if value is None:
        return None
    if (
        len(value) != _SHA256_HEX_LENGTH
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise AssetChecksumError("expected sha256 must be 64 lowercase hex characters")
    return value


def _validate_expected_size(value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise AssetChecksumError("expected size must be an integer")
    if value <= 0 or value > MAX_IMAGE_BYTES:
        raise AssetChecksumError("expected size is outside the allowed range")
    return value


def inspect_asset(path: Path) -> dict[str, Any]:
    """Return canonical size/hash metadata for one stable regular file.

    The pathname itself is intentionally omitted from the result. The caller
    must bind this digest to its own owner/object/version authority.
    """

    try:
        before = os.lstat(path)
    except OSError as exc:
        raise AssetChecksumError("asset is unavailable") from exc
    if not stat.S_ISREG(before.st_mode):
        raise AssetChecksumError("asset is not a regular file")

    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)

    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise AssetChecksumError("asset cannot be opened safely") from exc

    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise AssetChecksumError("asset is not a regular file")
        if opened.st_size <= 0 or opened.st_size > MAX_IMAGE_BYTES:
            raise AssetChecksumError("asset size is outside the allowed range")

        after_open = os.lstat(path)
        identity = (opened.st_dev, opened.st_ino)
        if (
            not stat.S_ISREG(after_open.st_mode)
            or (after_open.st_dev, after_open.st_ino) != identity
        ):
            raise AssetChecksumError("asset path changed during inspection")

        signature = _stat_signature(opened)
        digest = hashlib.sha256()
        remaining = opened.st_size
        while remaining:
            try:
                chunk = os.read(fd, min(_READ_CHUNK_BYTES, remaining))
            except OSError as exc:
                raise AssetChecksumError("asset cannot be read safely") from exc
            if not chunk:
                raise AssetChecksumError("asset changed during inspection")
            digest.update(chunk)
            remaining -= len(chunk)

        after_read = os.fstat(fd)
        if _stat_signature(after_read) != signature:
            raise AssetChecksumError("asset changed during inspection")

        after_path = os.lstat(path)
        if (
            not stat.S_ISREG(after_path.st_mode)
            or (after_path.st_dev, after_path.st_ino) != identity
        ):
            raise AssetChecksumError("asset path changed during inspection")

        return {
            "schema_version": 1,
            "algorithm": "sha256",
            "size_bytes": opened.st_size,
            "sha256": digest.hexdigest(),
        }
    except AssetChecksumError:
        raise
    except OSError as exc:
        raise AssetChecksumError("asset inspection failed") from exc
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def verify_asset(
    path: Path,
    *,
    expected_sha256: str | None = None,
    expected_size_bytes: int | None = None,
) -> dict[str, Any]:
    expected_digest = _validate_expected_sha256(expected_sha256)
    expected_size = _validate_expected_size(expected_size_bytes)
    if expected_digest is None and expected_size is None:
        raise AssetChecksumError("verification requires an expected sha256 or size")

    record = inspect_asset(path)
    if expected_size is not None and record["size_bytes"] != expected_size:
        raise AssetChecksumError("asset size does not match expected metadata")
    if expected_digest is not None and record["sha256"] != expected_digest:
        raise AssetChecksumError("asset sha256 does not match expected metadata")
    return {**record, "verified": True}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect or verify a bounded Node-local standby asset checksum."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("path", type=Path)

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("path", type=Path)
    verify_parser.add_argument("--expected-sha256")
    verify_parser.add_argument("--expected-size-bytes", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "inspect":
            result = inspect_asset(args.path)
        else:
            result = verify_asset(
                args.path,
                expected_sha256=args.expected_sha256,
                expected_size_bytes=args.expected_size_bytes,
            )
    except AssetChecksumError as exc:
        print(f"standby-asset-checksum: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
