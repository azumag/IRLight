#!/usr/bin/env python3
"""Validate persisted Node-capacity host preflight evidence without executing load."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

MAX_PREFLIGHT_BYTES = 64 * 1024
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
            raise HostPreflightEvidenceError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_snapshot(path: Path) -> dict[str, Any]:
    try:
        if not path.is_file() or path.is_symlink():
            raise HostPreflightEvidenceError("preflight evidence must be a regular non-symlink file")
        size_before = path.stat().st_size
        if size_before > MAX_PREFLIGHT_BYTES:
            raise HostPreflightEvidenceError("preflight evidence exceeds maximum size")
        raw_bytes = path.read_bytes()
        if len(raw_bytes) > MAX_PREFLIGHT_BYTES:
            raise HostPreflightEvidenceError("preflight evidence exceeds maximum size")
        if path.stat().st_size != size_before:
            raise HostPreflightEvidenceError("preflight evidence changed while reading")
        raw = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HostPreflightEvidenceError("preflight evidence must be valid UTF-8") from exc
    except OSError as exc:
        raise HostPreflightEvidenceError("preflight evidence could not be read") from exc

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
