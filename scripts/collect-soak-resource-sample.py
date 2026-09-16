#!/usr/bin/env python3
"""Collect one schema-v1 soak sample from a disposable IRLight Compose project.

This helper is intentionally read-only. It inspects the four PoC services with
Docker/Compose, aggregates resource/process observations, merges an explicitly
provided media-metrics snapshot, and prints one JSON sample accepted by
``validate-soak-report.py``.

It does not start, stop, or clean up containers, and it refuses arbitrary
Compose project names so an operator cannot accidentally aim the collector at
an unrelated project.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable


class SampleCollectionError(RuntimeError):
    """Raised when a trustworthy sample cannot be collected."""


EXPECTED_SERVICES = ("mediamtx", "continuity", "control-ui", "node-agent")
PROJECT_RE = re.compile(r"^irlight-poc-soak-[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
MEDIA_FIELDS = {
    "bitrate_bps",
    "av_sync_drift_ms",
    "timestamp_errors",
    "unexpected_reconnects",
}


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SampleCollectionError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise SampleCollectionError(f"non-standard JSON numeric constant: {value}")


def _finite_number(value: Any, label: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SampleCollectionError(f"{label} must be a finite number")
    try:
        normalized = float(value)
    except (OverflowError, TypeError, ValueError):
        raise SampleCollectionError(f"{label} must be a finite number") from None
    if not math.isfinite(normalized):
        raise SampleCollectionError(f"{label} must be a finite number")
    if minimum is not None and normalized < minimum:
        raise SampleCollectionError(f"{label} must be >= {minimum:g}")
    return normalized


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SampleCollectionError(f"{label} must be a non-negative integer")
    return value


def validate_project_name(project: str) -> str:
    if not PROJECT_RE.fullmatch(project):
        raise SampleCollectionError(
            "project must be a disposable IRLight soak project named irlight-poc-soak-*"
        )
    return project


def parse_cpu_percent(value: str) -> float:
    if not value.endswith("%"):
        raise SampleCollectionError(f"invalid Docker CPU percentage: {value!r}")
    try:
        normalized = float(value[:-1])
    except ValueError as exc:
        raise SampleCollectionError(f"invalid Docker CPU percentage: {value!r}") from exc
    if not math.isfinite(normalized) or normalized < 0:
        raise SampleCollectionError(f"invalid Docker CPU percentage: {value!r}")
    return normalized


def run_checked(argv: list[str], *, timeout: float = 15.0) -> str:
    try:
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SampleCollectionError(f"command failed to execute: {argv[0]}: {exc}") from exc
    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        detail = f": {stderr}" if stderr else ""
        raise SampleCollectionError(
            f"command exited {completed.returncode}: {argv[0]}{detail}"
        )
    return completed.stdout


def compose_service_ids(project: str, compose_file: Path) -> dict[str, str]:
    validate_project_name(project)
    result: dict[str, str] = {}
    for service in EXPECTED_SERVICES:
        output = run_checked(
            [
                "docker",
                "compose",
                "-p",
                project,
                "-f",
                str(compose_file),
                "ps",
                "-q",
                service,
            ]
        )
        ids = [line.strip() for line in output.splitlines() if line.strip()]
        if len(ids) != 1:
            raise SampleCollectionError(
                f"expected exactly one container for {service}, found {len(ids)}"
            )
        container_id = ids[0]
        running = run_checked(
            ["docker", "inspect", "--format", "{{.State.Running}}", container_id]
        ).strip()
        if running != "true":
            raise SampleCollectionError(f"service {service} is not running")
        result[service] = container_id
    if len(set(result.values())) != len(EXPECTED_SERVICES):
        raise SampleCollectionError("compose services resolved to duplicate container IDs")
    return result


def parse_stats_output(output: str, expected_count: int) -> float:
    lines = [line for line in output.splitlines() if line.strip()]
    if len(lines) != expected_count:
        raise SampleCollectionError(
            f"expected {expected_count} Docker stats rows, found {len(lines)}"
        )
    cpu_total = 0.0
    for index, line in enumerate(lines):
        try:
            value = json.loads(
                line,
                parse_constant=_reject_constant,
                object_pairs_hook=_strict_object,
            )
        except json.JSONDecodeError as exc:
            raise SampleCollectionError(f"invalid Docker stats JSON row {index}: {exc}") from exc
        if not isinstance(value, dict):
            raise SampleCollectionError(f"Docker stats row {index} must be an object")
        cpu_perc = value.get("CPUPerc")
        if not isinstance(cpu_perc, str):
            raise SampleCollectionError(
                f"Docker stats row {index} is missing string CPUPerc field"
            )
        cpu_total += parse_cpu_percent(cpu_perc)
    return cpu_total


def collect_cpu_percent(container_ids: list[str]) -> float:
    output = run_checked(
        [
            "docker",
            "stats",
            "--no-stream",
            "--format",
            "{{json .}}",
            *container_ids,
        ],
        timeout=30.0,
    )
    return parse_stats_output(output, len(container_ids))


def parse_top_output(output: str) -> list[tuple[int, str]]:
    result: list[tuple[int, str]] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1]:
            raise SampleCollectionError(f"unexpected docker top row: {line!r}")
        pid = int(parts[0])
        if pid <= 0:
            raise SampleCollectionError(f"invalid PID from docker top: {pid}")
        result.append((pid, parts[1]))
    if not result:
        raise SampleCollectionError("docker top returned no processes")
    return result


def _read_proc_observation(pid: int) -> tuple[int, int]:
    proc = Path("/proc") / str(pid)
    try:
        statm_fields = (proc / "statm").read_text(encoding="ascii").split()
        if len(statm_fields) < 2 or not statm_fields[1].isdigit():
            raise SampleCollectionError(f"invalid /proc/{pid}/statm resident page count")
        resident_pages = int(statm_fields[1])
        page_size = os.sysconf("SC_PAGE_SIZE")
        if isinstance(page_size, bool) or not isinstance(page_size, int) or page_size <= 0:
            raise SampleCollectionError("host page size is invalid")
        rss_bytes = resident_pages * page_size
        open_fds = sum(1 for _ in (proc / "fd").iterdir())
        return rss_bytes, open_fds
    except SampleCollectionError:
        raise
    except (FileNotFoundError, ProcessLookupError) as exc:
        raise SampleCollectionError(
            f"process {pid} disappeared while reading /proc observations"
        ) from exc
    except PermissionError as exc:
        raise SampleCollectionError(
            f"permission denied reading /proc observations for host PID {pid}"
        ) from exc
    except OSError as exc:
        raise SampleCollectionError(
            f"cannot read /proc observations for host PID {pid}: {exc}"
        ) from exc


def aggregate_processes(
    top_rows: list[tuple[int, str]],
    observer: Callable[[int], tuple[int, int]] = _read_proc_observation,
) -> tuple[int, int, int, int]:
    seen: set[int] = set()
    memory_rss_bytes = 0
    zombies = 0
    open_fds = 0
    for pid, state in top_rows:
        if pid in seen:
            raise SampleCollectionError(f"duplicate PID in docker top output: {pid}")
        seen.add(pid)
        if state.startswith("Z"):
            zombies += 1
        rss, fds = observer(pid)
        if isinstance(rss, bool) or not isinstance(rss, int) or rss < 0:
            raise SampleCollectionError(f"invalid RSS byte count for PID {pid}")
        if isinstance(fds, bool) or not isinstance(fds, int) or fds < 0:
            raise SampleCollectionError(f"invalid file-descriptor count for PID {pid}")
        memory_rss_bytes += rss
        open_fds += fds
    return memory_rss_bytes, len(seen), zombies, open_fds


def collect_process_observations(container_ids: list[str]) -> tuple[int, int, int, int]:
    all_rows: list[tuple[int, str]] = []
    for container_id in container_ids:
        output = run_checked(["docker", "top", container_id, "-eo", "pid=,stat="])
        all_rows.extend(parse_top_output(output))
    return aggregate_processes(all_rows)


def load_media_metrics(path: Path | None, *, allow_unmeasured: bool) -> dict[str, Any]:
    if path is None:
        if not allow_unmeasured:
            raise SampleCollectionError(
                "--media-metrics-file is required unless --allow-unmeasured-media is explicit"
            )
        return {
            "bitrate_bps": None,
            "av_sync_drift_ms": None,
            "timestamp_errors": 0,
            "unexpected_reconnects": 0,
        }
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SampleCollectionError(f"cannot read media metrics file: {exc}") from exc
    try:
        value = json.loads(
            raw,
            parse_constant=_reject_constant,
            object_pairs_hook=_strict_object,
        )
    except json.JSONDecodeError as exc:
        raise SampleCollectionError(f"invalid media metrics JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise SampleCollectionError("media metrics root must be an object")
    if set(value) != MEDIA_FIELDS:
        missing = sorted(MEDIA_FIELDS - set(value))
        extra = sorted(set(value) - MEDIA_FIELDS)
        details: list[str] = []
        if missing:
            details.append(f"missing={','.join(missing)}")
        if extra:
            details.append(f"extra={','.join(extra)}")
        raise SampleCollectionError(
            f"media metrics fields do not match schema ({'; '.join(details)})"
        )
    bitrate = value["bitrate_bps"]
    if bitrate is not None:
        bitrate = _finite_number(bitrate, "bitrate_bps", minimum=0.0)
    drift = value["av_sync_drift_ms"]
    if drift is not None:
        drift = _finite_number(drift, "av_sync_drift_ms")
    return {
        "bitrate_bps": bitrate,
        "av_sync_drift_ms": drift,
        "timestamp_errors": _nonnegative_int(value["timestamp_errors"], "timestamp_errors"),
        "unexpected_reconnects": _nonnegative_int(
            value["unexpected_reconnects"], "unexpected_reconnects"
        ),
    }


def build_sample(
    *,
    elapsed_seconds: float,
    memory_rss_bytes: int,
    cpu_percent: float,
    open_fds: int,
    processes: int,
    zombies: int,
    media_metrics: dict[str, Any],
) -> dict[str, Any]:
    elapsed = _finite_number(elapsed_seconds, "elapsed_seconds", minimum=0.0)
    memory = _nonnegative_int(memory_rss_bytes, "memory_rss_bytes")
    cpu = _finite_number(cpu_percent, "cpu_percent", minimum=0.0)
    fd_count = _nonnegative_int(open_fds, "open_fds")
    process_count = _nonnegative_int(processes, "processes")
    zombie_count = _nonnegative_int(zombies, "zombies")
    if zombie_count > process_count:
        raise SampleCollectionError("zombies cannot exceed processes")
    return {
        "elapsed_seconds": elapsed,
        "memory_rss_bytes": memory,
        "cpu_percent": cpu,
        "open_fds": fd_count,
        "processes": process_count,
        "zombies": zombie_count,
        "bitrate_bps": media_metrics["bitrate_bps"],
        "av_sync_drift_ms": media_metrics["av_sync_drift_ms"],
        "timestamp_errors": media_metrics["timestamp_errors"],
        "unexpected_reconnects": media_metrics["unexpected_reconnects"],
    }


def collect_sample(
    *,
    project: str,
    compose_file: Path,
    elapsed_seconds: float,
    media_metrics_file: Path | None,
    allow_unmeasured_media: bool,
) -> dict[str, Any]:
    service_ids = compose_service_ids(project, compose_file)
    ids = [service_ids[service] for service in EXPECTED_SERVICES]
    cpu = collect_cpu_percent(ids)
    memory, processes, zombies, fds = collect_process_observations(ids)
    media = load_media_metrics(media_metrics_file, allow_unmeasured=allow_unmeasured_media)
    return build_sample(
        elapsed_seconds=elapsed_seconds,
        memory_rss_bytes=memory,
        cpu_percent=cpu,
        open_fds=fds,
        processes=processes,
        zombies=zombies,
        media_metrics=media,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, help="disposable irlight-poc-soak-* Compose project")
    parser.add_argument(
        "--compose-file",
        type=Path,
        default=repo_root / "docker-compose.poc.yml",
        help="Compose file used by the soak project",
    )
    parser.add_argument("--elapsed-seconds", required=True, type=float)
    parser.add_argument("--media-metrics-file", type=Path)
    parser.add_argument(
        "--allow-unmeasured-media",
        action="store_true",
        help="explicitly emit null media gauges and zero counters for a resource-only sample",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        sample = collect_sample(
            project=args.project,
            compose_file=args.compose_file,
            elapsed_seconds=args.elapsed_seconds,
            media_metrics_file=args.media_metrics_file,
            allow_unmeasured_media=args.allow_unmeasured_media,
        )
    except SampleCollectionError as exc:
        print(f"soak sample collection failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(sample, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
