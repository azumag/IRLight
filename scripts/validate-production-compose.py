#!/usr/bin/env python3
"""Read-only safety validation for rendered production Media Node Compose config."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


REQUIRED_FILE_ENV = {
    "continuity": {
        "INPUT_URI_FILE",
        "EGRESS_URL_FILE",
    },
    "egress-gateway": {
        "EGRESS_INPUT_URI_FILE",
        "EGRESS_URL_FILE",
        "EGRESS_VERIFIED_PEER_IP_FILE",
    },
    "node-agent": {
        "NODE_BOOTSTRAP_TOKEN_FILE",
    },
}
REQUIRED_INTERNAL_NETWORKS = {"node-auth", "node-control"}
TMPFS_SECRET_VOLUMES = {
    "irlight-continuity-secrets",
    "irlight-relay-secrets",
    "irlight-egress-secrets",
}


def _environment_map(service: dict[str, Any]) -> dict[str, str]:
    raw = service.get("environment", {})
    if isinstance(raw, dict):
        return {
            str(key): "" if value is None else str(value)
            for key, value in raw.items()
        }
    if isinstance(raw, list):
        result: dict[str, str] = {}
        for item in raw:
            if not isinstance(item, str):
                continue
            key, separator, value = item.partition("=")
            result[key] = value if separator else ""
        return result
    return {}


def validate_rendered_config(config: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    services = config.get("services")
    if not isinstance(services, dict) or not services:
        return ["services must be a non-empty object"]

    for name, raw_service in services.items():
        if not isinstance(raw_service, dict):
            errors.append(f"service {name}: definition must be an object")
            continue
        service = raw_service

        if "build" in service:
            errors.append(f"service {name}: production config must not contain build")
        image = service.get("image")
        if not isinstance(image, str) or not image.strip():
            errors.append(f"service {name}: image is required")
        elif image.endswith(":latest"):
            errors.append(f"service {name}: :latest image tags are not allowed")

        if service.get("privileged") is True:
            errors.append(f"service {name}: privileged mode is not allowed")
        if service.get("network_mode") == "host":
            errors.append(f"service {name}: host network mode is not allowed")
        if service.get("pid") == "host":
            errors.append(f"service {name}: host pid namespace is not allowed")
        if service.get("ipc") == "host":
            errors.append(f"service {name}: host ipc namespace is not allowed")

    for service_name, required_keys in REQUIRED_FILE_ENV.items():
        service = services.get(service_name)
        if not isinstance(service, dict):
            errors.append(f"required service {service_name} is missing")
            continue
        environment = _environment_map(service)
        for key in sorted(required_keys):
            value = environment.get(key, "")
            if not value.startswith("/run/"):
                errors.append(
                    f"service {service_name}: {key} must reference a /run/ file"
                )

    networks = config.get("networks", {})
    if not isinstance(networks, dict):
        errors.append("networks must be an object")
    else:
        for network_name in sorted(REQUIRED_INTERNAL_NETWORKS):
            network = networks.get(network_name)
            if not isinstance(network, dict) or network.get("internal") is not True:
                errors.append(f"network {network_name}: internal=true is required")

    volumes = config.get("volumes", {})
    if not isinstance(volumes, dict):
        errors.append("volumes must be an object")
    else:
        for volume_name in sorted(TMPFS_SECRET_VOLUMES):
            volume = volumes.get(volume_name)
            if not isinstance(volume, dict):
                errors.append(f"volume {volume_name}: tmpfs secret volume is missing")
                continue
            driver_opts = volume.get("driver_opts")
            if not isinstance(driver_opts, dict):
                errors.append(f"volume {volume_name}: driver_opts are required")
                continue
            if str(driver_opts.get("type", "")) != "tmpfs":
                errors.append(f"volume {volume_name}: type=tmpfs is required")
            mount_opts = str(driver_opts.get("o", ""))
            if "mode=700" not in mount_opts.split(","):
                errors.append(f"volume {volume_name}: mode=700 is required")

    node_agent = services.get("node-agent")
    if isinstance(node_agent, dict):
        docker_socket_ok = False
        service_volumes = node_agent.get("volumes", [])
        if isinstance(service_volumes, list):
            for volume in service_volumes:
                if isinstance(volume, dict):
                    source = str(volume.get("source", ""))
                    target = str(volume.get("target", ""))
                    read_only = volume.get("read_only") is True
                    if (
                        source == "/var/run/docker.sock"
                        and target == "/var/run/docker.sock"
                        and read_only
                    ):
                        docker_socket_ok = True
                elif isinstance(volume, str):
                    if volume == "/var/run/docker.sock:/var/run/docker.sock:ro":
                        docker_socket_ok = True
        if not docker_socket_ok:
            errors.append("service node-agent: Docker socket must be mounted read-only")

    return errors


def render_compose(files: list[Path], *, timeout_seconds: float) -> dict[str, Any]:
    command = ["docker", "compose"]
    for compose_file in files:
        command.extend(["-f", str(compose_file)])
    command.extend(["config", "--format", "json"])

    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"docker compose config failed: {exc}") from exc

    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        raise RuntimeError(f"docker compose config exited {result.returncode}{suffix}")

    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("docker compose config returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError("docker compose config returned a non-object")
    return value


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Render Compose without starting containers and validate IRLight "
            "production Media Node safety invariants."
        )
    )
    parser.add_argument(
        "-f",
        "--file",
        action="append",
        dest="files",
        required=True,
        help="Compose file; repeat in the same order used for deployment.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=20.0,
        help="Maximum time allowed for docker compose config (default: 20).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if args.timeout_seconds <= 0:
        print("ERROR: --timeout-seconds must be positive", file=sys.stderr)
        return 2

    files = [Path(value) for value in args.files]
    missing = [str(path) for path in files if not path.is_file()]
    if missing:
        for path in missing:
            print(f"ERROR: compose file not found: {path}", file=sys.stderr)
        return 2

    try:
        config = render_compose(files, timeout_seconds=args.timeout_seconds)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    errors = validate_rendered_config(config)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print(
        "OK: rendered Compose passed read-only production safety validation "
        f"({len(config['services'])} services)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
