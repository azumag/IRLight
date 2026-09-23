#!/usr/bin/env python3
"""Check local host prerequisites before a measured Node-capacity run.

This helper is deliberately read-only. It captures only a small, non-secret set
of host/runtime facts needed to tell whether the local machine is a plausible
place to run the existing Node-capacity harness. It never starts containers,
contacts a provider, reads credentials, or changes scheduler/Node state.
"""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable


class HostPreflightError(RuntimeError):
    """Raised when the host cannot provide trustworthy preflight evidence."""


MAX_MEMINFO_BYTES = 64 * 1024
SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+:-]{0,127}$")


def _safe_value(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise HostPreflightError(f"{label} is missing or contains unsafe characters")
    normalized = value.strip()
    if not SAFE_VALUE_RE.fullmatch(normalized):
        raise HostPreflightError(f"{label} is missing or contains unsafe characters")
    return normalized


def _docker_host_from_environment() -> str | None:
    return os.environ.get("DOCKER_HOST")


def _require_local_docker_endpoint(value: object) -> None:
    if not isinstance(value, str):
        raise HostPreflightError("Docker endpoint is unavailable")
    endpoint = value.strip()
    if not endpoint.startswith("unix:///") or len(endpoint) > 512:
        raise HostPreflightError("Node-capacity preflight refuses a non-local Docker endpoint")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in endpoint):
        raise HostPreflightError("Docker endpoint contains unsafe characters")


def parse_meminfo(text: str) -> int:
    """Return MemTotal in bytes from a Linux /proc/meminfo snapshot."""

    memory_kib: int | None = None
    for raw_line in text.splitlines():
        key, separator, remainder = raw_line.partition(":")
        if not separator or key != "MemTotal":
            continue
        if memory_kib is not None:
            raise HostPreflightError("/proc/meminfo contains duplicate MemTotal")
        fields = remainder.split()
        if len(fields) != 2 or fields[1] != "kB" or not fields[0].isdigit():
            raise HostPreflightError("/proc/meminfo has an invalid MemTotal")
        memory_kib = int(fields[0])

    if memory_kib is None or memory_kib <= 0:
        raise HostPreflightError("/proc/meminfo is missing a positive MemTotal")
    return memory_kib * 1024


def read_meminfo(path: Path = Path("/proc/meminfo")) -> str:
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_MEMINFO_BYTES + 1)
    except OSError as exc:
        raise HostPreflightError("cannot read /proc/meminfo") from exc
    if len(raw) > MAX_MEMINFO_BYTES:
        raise HostPreflightError("/proc/meminfo exceeds the preflight read limit")
    try:
        return raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise HostPreflightError("/proc/meminfo is not ASCII") from exc


def run_checked(argv: list[str], *, timeout: float = 10.0) -> str:
    try:
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HostPreflightError(f"cannot execute {argv[0]} preflight command") from exc
    if completed.returncode != 0:
        raise HostPreflightError(f"{argv[0]} preflight command failed")
    return completed.stdout


def collect_snapshot(
    *,
    system_name: Callable[[], str] = platform.system,
    machine_name: Callable[[], str] = platform.machine,
    kernel_release: Callable[[], str] = platform.release,
    logical_cpu_count: Callable[[], int | None] = os.cpu_count,
    meminfo_reader: Callable[[], str] = read_meminfo,
    docker_host_from_environment: Callable[[], str | None] = _docker_host_from_environment,
    runner: Callable[[list[str]], str] = run_checked,
) -> dict[str, object]:
    system = system_name()
    if system != "Linux":
        raise HostPreflightError("Node-capacity host preflight requires Linux")

    cpus = logical_cpu_count()
    if isinstance(cpus, bool) or not isinstance(cpus, int) or cpus <= 0:
        raise HostPreflightError("logical CPU count is unavailable or invalid")

    memory_total_bytes = parse_meminfo(meminfo_reader())
    machine = _safe_value(machine_name(), "machine architecture")
    kernel = _safe_value(kernel_release(), "kernel release")

    environment_endpoint = docker_host_from_environment()
    if environment_endpoint:
        _require_local_docker_endpoint(environment_endpoint)
    else:
        context_endpoint = runner(
            ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"]
        )
        _require_local_docker_endpoint(context_endpoint)

    docker_server = _safe_value(
        runner(["docker", "version", "--format", "{{.Server.Version}}"]).strip(),
        "Docker server version",
    )
    compose_version = _safe_value(
        runner(["docker", "compose", "version", "--short"]).strip(),
        "Docker Compose version",
    )

    return {
        "schema_version": 1,
        "kind": "irlight-node-capacity-host-preflight",
        "ready": True,
        "platform": {
            "system": system,
            "machine": machine,
            "kernel_release": kernel,
        },
        "resources": {
            "logical_cpu_count": cpus,
            "memory_total_bytes": memory_total_bytes,
        },
        "docker": {
            "server_version": docker_server,
            "compose_version": compose_version,
        },
    }


def main() -> int:
    try:
        snapshot = collect_snapshot()
    except HostPreflightError as exc:
        print(f"Node-capacity host preflight failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(snapshot, sort_keys=True, separators=(",", ":"), allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
