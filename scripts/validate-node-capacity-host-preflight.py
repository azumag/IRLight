#!/usr/bin/env python3
"""Validate persisted Node-capacity host preflight evidence without executing load."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any

MAX_PREFLIGHT_BYTES = 64 * 1024
READ_CHUNK_BYTES = 64 * 1024
SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+:-]{0,127}$")
EXPECTED_FIELDS = {
    "schema_version",
    "kind",
    "ready",
    "platform",
    "resources",
    "docker",
}


class HostPreflightEvidenceError(ValueError):
    """Raised when host preflight evidence is malformed or unsafe."""


def _reject_constant(value: str) -> None:
    raise HostPreflightEvidenceError(f"non-standard JSON numeric constant: {value}")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HostPreflightEvidenceError("preflight evidence contains a duplicate JSON key")
        result[key] = value
    return result


def _identity(snapshot: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        snapshot.st_dev,
        snapshot.st_ino,
        snapshot.st_size,
        snapshot.st_mtime_ns,
        snapshot.st_ctime_ns,
    )


def _read_evidence_bytes(path: Path) -> bytes:
    """Read one bounded stable regular file without following a final symlink."""

    try:
        before = os.lstat(path)
    except OSError as exc:
        raise HostPreflightEvidenceError("preflight evidence could not be inspected") from exc
    if not stat.S_ISREG(before.st_mode):
        raise HostPreflightEvidenceError("preflight evidence must be a regular file")
    if before.st_size > MAX_PREFLIGHT_BYTES:
        raise HostPreflightEvidenceError("preflight evidence exceeds maximum size")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise HostPreflightEvidenceError("preflight evidence could not be opened") from exc

    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise HostPreflightEvidenceError("preflight evidence must be a regular file")
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise HostPreflightEvidenceError("preflight evidence changed while opening")
        if opened.st_size > MAX_PREFLIGHT_BYTES:
            raise HostPreflightEvidenceError("preflight evidence exceeds maximum size")

        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, READ_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_PREFLIGHT_BYTES:
                raise HostPreflightEvidenceError("preflight evidence exceeds maximum size")
            chunks.append(chunk)

        after_read = os.fstat(fd)
        try:
            after_path = os.lstat(path)
        except OSError as exc:
            raise HostPreflightEvidenceError("preflight evidence changed while reading") from exc
        if not stat.S_ISREG(after_path.st_mode):
            raise HostPreflightEvidenceError("preflight evidence changed while reading")
        if _identity(opened) != _identity(after_read) or _identity(opened) != _identity(after_path):
            raise HostPreflightEvidenceError("preflight evidence changed while reading")
        return b"".join(chunks)
    finally:
        os.close(fd)


def load_snapshot(path: Path) -> dict[str, Any]:
    try:
        raw = _read_evidence_bytes(path).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HostPreflightEvidenceError("preflight evidence must be valid UTF-8") from exc

    try:
        value = json.loads(raw, parse_constant=_reject_constant, object_pairs_hook=_strict_object)
    except json.JSONDecodeError as exc:
        raise HostPreflightEvidenceError("preflight evidence must be valid JSON") from exc
    except RecursionError as exc:
        raise HostPreflightEvidenceError("preflight evidence JSON nesting is too deep") from exc
    if not isinstance(value, dict):
        raise HostPreflightEvidenceError("preflight evidence root must be an object")
    return value


def _bounded_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not SAFE_VALUE_RE.fullmatch(value):
        raise HostPreflightEvidenceError(f"preflight evidence contains an invalid {label}")
    return value


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise HostPreflightEvidenceError(f"preflight evidence contains an invalid {label}")
    return value


def validate_snapshot(snapshot: object) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        raise HostPreflightEvidenceError("preflight evidence snapshot is invalid")
    if set(snapshot) != EXPECTED_FIELDS:
        raise HostPreflightEvidenceError("preflight evidence snapshot shape is invalid")

    schema_version = snapshot.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version != 1:
        raise HostPreflightEvidenceError("preflight evidence schema is unsupported")
    if snapshot.get("kind") != "irlight-node-capacity-host-preflight":
        raise HostPreflightEvidenceError("preflight evidence kind is unexpected")
    if snapshot.get("ready") is not True:
        raise HostPreflightEvidenceError("preflight evidence is not ready")

    platform = snapshot.get("platform")
    if not isinstance(platform, dict) or set(platform) != {"system", "machine", "kernel_release"}:
        raise HostPreflightEvidenceError("preflight evidence platform snapshot is invalid")
    if platform.get("system") != "Linux":
        raise HostPreflightEvidenceError("preflight evidence platform is unexpected")
    _bounded_text(platform.get("machine"), "machine architecture")
    _bounded_text(platform.get("kernel_release"), "kernel release")

    resources = snapshot.get("resources")
    if not isinstance(resources, dict) or set(resources) != {"logical_cpu_count", "memory_total_bytes"}:
        raise HostPreflightEvidenceError("preflight evidence resource snapshot is invalid")
    _positive_int(resources.get("logical_cpu_count"), "logical CPU count")
    _positive_int(resources.get("memory_total_bytes"), "total memory")

    docker = snapshot.get("docker")
    if not isinstance(docker, dict) or set(docker) != {"server_version", "compose_version"}:
        raise HostPreflightEvidenceError("preflight evidence Docker snapshot is invalid")
    _bounded_text(docker.get("server_version"), "Docker server version")
    _bounded_text(docker.get("compose_version"), "Docker Compose version")
    return snapshot


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path, help="Persisted host preflight JSON evidence")
    parser.add_argument("--json", action="store_true", help="Emit a deterministic validation summary")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        snapshot = validate_snapshot(load_snapshot(args.evidence))
    except HostPreflightEvidenceError as exc:
        print(f"node capacity host preflight evidence validation failed: {exc}", file=sys.stderr)
        return 2

    if args.json:
        json.dump(
            {"valid": True, "schema_version": snapshot["schema_version"], "kind": snapshot["kind"]},
            sys.stdout,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        sys.stdout.write("\n")
    else:
        print("node capacity host preflight evidence valid: schema_version=1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
