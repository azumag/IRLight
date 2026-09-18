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
import stat
import subprocess
from pathlib import Path
from typing import Any


class MediaStackInspectError(RuntimeError):
    """Raised when the media stack cannot be inspected safely."""


MAX_RESTART_BASELINE_BYTES = 1024 * 1024


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


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _open_restart_baseline_readonly(path: Path) -> Any:
    """Open one bounded stable regular baseline without following its final path."""

    try:
        before = os.lstat(path)
    except OSError as exc:
        raise MediaStackInspectError("restart baseline is unavailable") from exc
    if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_RESTART_BASELINE_BYTES:
        raise MediaStackInspectError("restart baseline is unavailable")

    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise MediaStackInspectError("restart baseline is unavailable") from exc
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_size > MAX_RESTART_BASELINE_BYTES
            or _file_identity(before) != _file_identity(opened)
        ):
            raise MediaStackInspectError("restart baseline is unavailable")
        return os.fdopen(fd, "rb")
    except Exception:
        os.close(fd)
        raise


def _read_restart_baseline_bytes(path: Path) -> bytes:
    try:
        with _open_restart_baseline_readonly(path) as handle:
            before_read = os.fstat(handle.fileno())
            raw = handle.read(MAX_RESTART_BASELINE_BYTES + 1)
            after_read = os.fstat(handle.fileno())
    except OSError as exc:
        raise MediaStackInspectError("restart baseline is unavailable") from exc

    if (
        len(raw) > MAX_RESTART_BASELINE_BYTES
        or _file_identity(before_read) != _file_identity(after_read)
    ):
        raise MediaStackInspectError("restart baseline is unavailable")
    try:
        final_path = os.lstat(path)
    except OSError as exc:
        raise MediaStackInspectError("restart baseline is unavailable") from exc
    if (
        not stat.S_ISREG(final_path.st_mode)
        or _file_identity(final_path) != _file_identity(after_read)
    ):
        raise MediaStackInspectError("restart baseline is unavailable")
    return raw


def _load_restart_baseline(
    path: Path, *, egress_mode: str, services: tuple[str, ...]
) -> dict[str, int]:
    try:
        payload = json.loads(_read_restart_baseline_bytes(path).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise MediaStackInspectError("restart baseline is unavailable") from exc

    if not isinstance(payload, dict) or payload.get("egress_mode") != egress_mode:
        raise MediaStackInspectError("restart baseline is incompatible")

    rows = payload.get("services")
    if not isinstance(rows, list):
        raise MediaStackInspectError("restart baseline is incompatible")

    expected = set(services)
    baseline: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise MediaStackInspectError("restart baseline is incompatible")
        service = row.get("service")
        restart_count = row.get("restart_count")
        if (
            not isinstance(service, str)
            or service not in expected
            or service in baseline
            or isinstance(restart_count, bool)
            or not isinstance(restart_count, int)
            or restart_count < 0
        ):
            raise MediaStackInspectError("restart baseline is incompatible")
        baseline[service] = restart_count

    if set(baseline) != expected:
        raise MediaStackInspectError("restart baseline is incompatible")
    return baseline


def inspect_media_stack(
    *,
    compose_file: Path,
    project_name: str,
    egress_mode: str,
    timeout_seconds: float,
    restart_warning_count: int,
    restart_baseline: Path | None = None,
) -> dict[str, Any]:
    if not compose_file.is_file():
        raise MediaStackInspectError("compose control file is unavailable")

    services = expected_services(egress_mode)
    baseline = (
        _load_restart_baseline(
            restart_baseline, egress_mode=egress_mode, services=services
        )
        if restart_baseline is not None
        else None
    )
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
            summary: dict[str, Any] = {
                "service": service,
                "state": "missing",
                "health": None,
                "exit_code": None,
                "oom_killed": None,
                "restart_count": None,
                "problems": ["missing"],
                "warnings": [],
            }
            if baseline is not None:
                summary["restart_delta"] = None
            summaries.append(summary)
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

        restart_delta: int | None = None
        if baseline is None:
            if restart_count >= restart_warning_count:
                warnings.append("restart_history_high")
        else:
            previous_restart_count = baseline[service]
            if restart_count < previous_restart_count:
                warnings.append("restart_baseline_generation_mismatch")
            else:
                restart_delta = restart_count - previous_restart_count
                if restart_delta > 0:
                    warnings.append("restart_delta_detected")

        problem_count += len(problems)
        warning_count += len(warnings)
        summary = {
            "service": service,
            "state": state,
            "health": health or None,
            "exit_code": exit_code,
            "oom_killed": oom_killed,
            "restart_count": restart_count,
            "problems": problems,
            "warnings": warnings,
        }
        if baseline is not None:
            summary["restart_delta"] = restart_delta
        summaries.append(summary)

    status = "PROBLEM" if problem_count else "WARNING" if warning_count else "OK"
    result: dict[str, Any] = {
        "status": status,
        "egress_mode": egress_mode,
        "problem_count": problem_count,
        "warning_count": warning_count,
        "restart_warning_count": restart_warning_count,
        "services": summaries,
    }
    if baseline is not None:
        result["restart_baseline_mode"] = True
    return result


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
    parser.add_argument(
        "--restart-baseline",
        default=os.getenv("NODE_RESTART_BASELINE_PATH"),
        help=(
            "Previous redacted inspector JSON for opt-in restart-count delta "
            "(default: NODE_RESTART_BASELINE_PATH)"
        ),
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
            restart_baseline=Path(args.restart_baseline)
            if args.restart_baseline
            else None,
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
