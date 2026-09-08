"""Read-only Media Node process inspection for operators.

The inspector only invokes Docker read operations and emits a whitelisted,
redacted summary. It never starts, stops, recreates, or removes containers and
never prints container environment, command lines, mounts, or secret files.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
from pathlib import Path
from typing import Any


class MediaStackInspectError(RuntimeError):
    """Raised when the media stack cannot be inspected safely."""


def _positive_finite(value: str) -> float:
    try:
        numeric = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(numeric) or numeric <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return numeric


def _positive_int(value: str) -> int:
    try:
        numeric = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if numeric <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return numeric


def _gateway_enabled() -> bool:
    configured = os.getenv("EGRESS_GATEWAY_ENABLED", "1").strip().lower()
    return configured not in {"0", "false", "no", "off"}


def expected_services(egress_mode: str) -> tuple[str, ...]:
    if egress_mode not in {"DIRECT_PUSH", "RELAY_ONLY"}:
        raise ValueError("unsupported egress mode")
    services = ["mediamtx", "continuity"]
    if egress_mode != "RELAY_ONLY" and _gateway_enabled():
        services.append("egress-gateway")
    return tuple(services)


def _run_readonly(
    command: list[str], *, timeout_seconds: float, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MediaStackInspectError("docker inspection command failed") from exc


def parse_compose_ps(output: str) -> list[dict[str, Any]]:
    payload = output.strip()
    if not payload:
        return []
    try:
        if payload.startswith("["):
            parsed = json.loads(payload)
            if not isinstance(parsed, list) or any(
                not isinstance(item, dict) for item in parsed
            ):
                raise ValueError("compose ps JSON must be a list of objects")
            return parsed

        rows: list[dict[str, Any]] = []
        for line in payload.splitlines():
            item = json.loads(line)
            if not isinstance(item, dict):
                raise ValueError("compose ps row must be an object")
            rows.append(item)
        return rows
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise MediaStackInspectError("docker compose ps returned invalid JSON") from exc


def _parse_inspect_line(output: str) -> tuple[str, int, bool, int]:
    fields = output.strip().split("\t")
    if len(fields) != 4:
        raise MediaStackInspectError("docker inspect returned invalid state")
    state = fields[0].strip().lower()
    try:
        exit_code = int(fields[1])
        restart_count = int(fields[3])
    except ValueError as exc:
        raise MediaStackInspectError("docker inspect returned invalid numeric state") from exc
    oom_value = fields[2].strip().lower()
    if oom_value not in {"true", "false"} or exit_code < 0 or restart_count < 0:
        raise MediaStackInspectError("docker inspect returned invalid state")
    return state, exit_code, oom_value == "true", restart_count


def inspect_media_stack(
    *,
    compose_file: Path,
    project_name: str,
    egress_mode: str,
    timeout_seconds: float,
    restart_warning_count: int,
) -> dict[str, Any]:
    if not compose_file.is_file():
        raise MediaStackInspectError("compose control file is unavailable")

    services = expected_services(egress_mode)
    env = dict(os.environ)
    env["COMPOSE_PROJECT_NAME"] = project_name
    ps = _run_readonly(
        [
            "docker",
            "compose",
            "-f",
            str(compose_file),
            "ps",
            "--all",
            "--format",
            "json",
            *services,
        ],
        timeout_seconds=timeout_seconds,
        env=env,
    )
    if ps.returncode != 0:
        raise MediaStackInspectError("docker compose ps is unavailable")

    rows = parse_compose_ps(ps.stdout)
    by_service = {
        str(row.get("Service", "")): row
        for row in rows
        if isinstance(row.get("Service"), str) and row.get("Service")
    }

    summaries: list[dict[str, Any]] = []
    problem_count = 0
    warning_count = 0

    for service in services:
        row = by_service.get(service)
        if row is None:
            problem_count += 1
            summaries.append(
                {
                    "service": service,
                    "state": "missing",
                    "health": None,
                    "exit_code": None,
                    "oom_killed": None,
                    "restart_count": None,
                    "problems": ["missing"],
                    "warnings": [],
                }
            )
            continue

        container_name = row.get("Name")
        if not isinstance(container_name, str) or not container_name.strip():
            raise MediaStackInspectError("compose ps omitted container identity")

        # Keep this format deliberately narrow. Full `docker inspect` JSON can
        # expose environment values, process arguments, mounts, and secrets.
        inspected = _run_readonly(
            [
                "docker",
                "inspect",
                "--format",
                "{{.State.Status}}\t{{.State.ExitCode}}\t{{.State.OOMKilled}}\t{{.RestartCount}}",
                container_name,
            ],
            timeout_seconds=timeout_seconds,
        )
        if inspected.returncode != 0:
            raise MediaStackInspectError("docker container state is unavailable")
        state, exit_code, oom_killed, restart_count = _parse_inspect_line(
            inspected.stdout
        )

        health_raw = row.get("Health")
        health = str(health_raw).strip().lower() if health_raw is not None else ""
        problems: list[str] = []
        warnings: list[str] = []

        if state in {"restarting", "dead", "removing"}:
            problems.append(f"state_{state}")
        elif state != "running":
            problems.append("not_running")
        if oom_killed:
            problems.append("oom_killed")
        if health == "unhealthy":
            problems.append("unhealthy")
        elif health == "starting":
            warnings.append("health_starting")
        if restart_count >= restart_warning_count:
            warnings.append("restart_history_high")

        problem_count += len(problems)
        warning_count += len(warnings)
        summaries.append(
            {
                "service": service,
                "state": state,
                "health": health or None,
                "exit_code": exit_code,
                "oom_killed": oom_killed,
                "restart_count": restart_count,
                "problems": problems,
                "warnings": warnings,
            }
        )

    status = "PROBLEM" if problem_count else "WARNING" if warning_count else "OK"
    return {
        "status": status,
        "egress_mode": egress_mode,
        "problem_count": problem_count,
        "warning_count": warning_count,
        "restart_warning_count": restart_warning_count,
        "services": summaries,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="media_stack_inspect_cli")
    parser.add_argument(
        "--compose-file",
        default=os.getenv("NODE_COMPOSE_FILE", "docker-compose.control.yml"),
        help="Media-only Compose control file (default: NODE_COMPOSE_FILE)",
    )
    parser.add_argument(
        "--project-name",
        default=os.getenv("NODE_COMPOSE_PROJECT", "irlight-node"),
        help="Compose project name (default: NODE_COMPOSE_PROJECT or irlight-node)",
    )
    parser.add_argument(
        "--egress-mode",
        choices=("DIRECT_PUSH", "RELAY_ONLY"),
        default=os.getenv("NODE_EGRESS_MODE"),
        help="Current Session egress mode; required unless NODE_EGRESS_MODE is set",
    )
    parser.add_argument(
        "--command-timeout-seconds",
        type=_positive_finite,
        default=os.getenv("NODE_DOCKER_COMMAND_TIMEOUT_SECONDS", "15"),
        help="Per-command Docker inspection timeout (default: env or 15)",
    )
    parser.add_argument(
        "--restart-warning-count",
        type=_positive_int,
        default=os.getenv("NODE_RESTART_WARNING_COUNT", "3"),
        help="Lifetime restart count that warrants operator correlation (default: env or 3)",
    )
    args = parser.parse_args(argv)
    if args.egress_mode is None:
        parser.error("--egress-mode is required unless NODE_EGRESS_MODE is set")

    try:
        payload = inspect_media_stack(
            compose_file=Path(args.compose_file),
            project_name=args.project_name,
            egress_mode=args.egress_mode,
            timeout_seconds=args.command_timeout_seconds,
            restart_warning_count=args.restart_warning_count,
        )
    except MediaStackInspectError:
        print(
            json.dumps(
                {"status": "UNAVAILABLE", "reason": "media stack inspection unavailable"},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 3

    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    if payload["problem_count"]:
        return 2
    if payload["warning_count"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
