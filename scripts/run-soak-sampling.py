#!/usr/bin/env python3
"""Collect a durable JSONL series of soak samples from an already-running project.

This runner only orchestrates ``collect-soak-resource-sample.py``. It does not
start, stop, restart, or remove containers, and it does not claim that a run
passed product acceptance thresholds. The output remains useful after an
interruption because each successfully validated sample is flushed and fsynced
before the next scheduled observation.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, TextIO


class SoakSamplingError(RuntimeError):
    """Raised when a trustworthy bounded sampling run cannot continue."""


PROJECT_RE = re.compile(r"^irlight-poc-soak-[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
DEFAULT_COLLECTOR_TIMEOUT_SECONDS = 180.0
MAX_COLLECTOR_STDOUT_BYTES = 64 * 1024
MAX_COLLECTOR_STDERR_BYTES = 64 * 1024


def _finite_nonnegative(value: object, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SoakSamplingError(f"{label} must be a finite number")
    try:
        normalized = float(value)
    except (OverflowError, TypeError, ValueError):
        raise SoakSamplingError(f"{label} must be a finite number") from None
    if not math.isfinite(normalized):
        raise SoakSamplingError(f"{label} must be a finite number")
    minimum_ok = normalized > 0.0 if positive else normalized >= 0.0
    if not minimum_ok:
        relation = "> 0" if positive else ">= 0"
        raise SoakSamplingError(f"{label} must be {relation}")
    return normalized


def validate_project_name(project: str) -> str:
    if not PROJECT_RE.fullmatch(project):
        raise SoakSamplingError(
            "project must be a disposable IRLight soak project named irlight-poc-soak-*"
        )
    return project


def sample_targets(duration_seconds: float, interval_seconds: float) -> list[float]:
    duration = _finite_nonnegative(duration_seconds, "duration_seconds", positive=True)
    interval = _finite_nonnegative(interval_seconds, "interval_seconds", positive=True)
    targets = [0.0]
    next_target = interval
    while next_target < duration:
        targets.append(next_target)
        try:
            next_target += interval
        except OverflowError:
            break
        if not math.isfinite(next_target):
            break
    targets.append(duration)
    return targets


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SoakSamplingError(f"collector returned duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise SoakSamplingError(f"collector returned non-standard JSON number: {value}")


def parse_collector_output(stdout: str, *, expected_elapsed: float) -> dict[str, Any]:
    if len(stdout.encode("utf-8")) > MAX_COLLECTOR_STDOUT_BYTES:
        raise SoakSamplingError("collector stdout exceeds bounded size")
    lines = [line for line in stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise SoakSamplingError("collector must emit exactly one non-empty JSON line")
    try:
        sample = json.loads(
            lines[0],
            parse_constant=_reject_constant,
            object_pairs_hook=_strict_object,
        )
    except (json.JSONDecodeError, RecursionError) as exc:
        raise SoakSamplingError("collector returned invalid JSON") from exc
    if not isinstance(sample, dict):
        raise SoakSamplingError("collector JSON root must be an object")
    elapsed = _finite_nonnegative(sample.get("elapsed_seconds"), "collector elapsed_seconds")
    if not math.isclose(elapsed, expected_elapsed, rel_tol=0.0, abs_tol=1e-6):
        raise SoakSamplingError("collector elapsed_seconds does not match requested observation")
    return sample


def build_collector_argv(
    *,
    collector_path: Path,
    project: str,
    compose_file: Path,
    elapsed_seconds: float,
    media_metrics_file: Path | None,
    allow_unmeasured_media: bool,
) -> list[str]:
    validate_project_name(project)
    elapsed = _finite_nonnegative(elapsed_seconds, "elapsed_seconds")
    argv = [
        sys.executable,
        str(collector_path),
        "--project",
        project,
        "--compose-file",
        str(compose_file),
        "--elapsed-seconds",
        repr(elapsed),
    ]
    if media_metrics_file is not None:
        argv.extend(["--media-metrics-file", str(media_metrics_file)])
    elif allow_unmeasured_media:
        argv.append("--allow-unmeasured-media")
    else:
        raise SoakSamplingError(
            "--media-metrics-file is required unless --allow-unmeasured-media is explicit"
        )
    return argv


def collect_one_process(
    argv: list[str],
    *,
    expected_elapsed: float,
    timeout_seconds: float,
) -> dict[str, Any]:
    timeout = _finite_nonnegative(timeout_seconds, "collector_timeout_seconds", positive=True)
    try:
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise SoakSamplingError("collector exceeded its bounded execution time") from exc
    except OSError as exc:
        raise SoakSamplingError("collector could not be executed") from exc

    stderr = completed.stderr
    if len(stderr.encode("utf-8")) > MAX_COLLECTOR_STDERR_BYTES:
        stderr = stderr.encode("utf-8")[:MAX_COLLECTOR_STDERR_BYTES].decode(
            "utf-8", errors="replace"
        )
    if completed.returncode != 0:
        # The child collector already avoids secrets by contract. Still keep
        # runner diagnostics bounded and do not echo argv (which can contain
        # operator-local paths).
        detail = stderr.strip()
        suffix = f": {detail}" if detail else ""
        raise SoakSamplingError(f"collector exited {completed.returncode}{suffix}")
    return parse_collector_output(completed.stdout, expected_elapsed=expected_elapsed)


def _validated_monotonic(value: object) -> float:
    return _finite_nonnegative(value, "monotonic clock")


def wait_until(
    deadline: float,
    *,
    started_at: float,
    monotonic: Callable[[], float],
    sleeper: Callable[[float], None],
) -> float:
    checked_deadline = _validated_monotonic(deadline)
    start = _validated_monotonic(started_at)
    if checked_deadline < start:
        raise SoakSamplingError("sampling deadline moved before run start")
    while True:
        now = _validated_monotonic(monotonic())
        if now < start:
            raise SoakSamplingError("monotonic clock moved backwards before run start")
        remaining = checked_deadline - now
        if remaining <= 0.0:
            return now - start
        sleeper(remaining)


def _write_sample(output: TextIO, sample: dict[str, Any]) -> None:
    try:
        encoded = json.dumps(
            sample,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise SoakSamplingError("collector sample cannot be serialized safely") from exc
    output.write(encoded + "\n")
    output.flush()
    try:
        os.fsync(output.fileno())
    except (AttributeError, OSError) as exc:
        raise SoakSamplingError("cannot durably flush soak sample output") from exc


def run_sampling(
    *,
    duration_seconds: float,
    interval_seconds: float,
    collect: Callable[[float], dict[str, Any]],
    output: TextIO,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    targets = sample_targets(duration_seconds, interval_seconds)
    started_at = _validated_monotonic(monotonic())

    baseline = collect(0.0)
    if not isinstance(baseline, dict):
        raise SoakSamplingError("collector did not return an object")
    baseline_elapsed = _finite_nonnegative(
        baseline.get("elapsed_seconds"), "collector elapsed_seconds"
    )
    if baseline_elapsed != 0.0:
        raise SoakSamplingError("baseline sample must use elapsed_seconds=0")
    _write_sample(output, baseline)

    written = 1
    for target in targets[1:]:
        try:
            deadline = started_at + target
        except OverflowError:
            raise SoakSamplingError("sampling deadline overflowed") from None
        if not math.isfinite(deadline):
            raise SoakSamplingError("sampling deadline overflowed")
        elapsed = wait_until(
            deadline,
            started_at=started_at,
            monotonic=monotonic,
            sleeper=sleeper,
        )
        sample = collect(elapsed)
        if not isinstance(sample, dict):
            raise SoakSamplingError("collector did not return an object")
        observed = _finite_nonnegative(
            sample.get("elapsed_seconds"), "collector elapsed_seconds"
        )
        if not math.isclose(observed, elapsed, rel_tol=0.0, abs_tol=1e-6):
            raise SoakSamplingError(
                "collector elapsed_seconds does not match scheduled observation"
            )
        _write_sample(output, sample)
        written += 1
    return written


def open_output_exclusive(path: Path) -> TextIO:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise SoakSamplingError("output file already exists; refusing to overwrite evidence") from exc
    except OSError as exc:
        raise SoakSamplingError("cannot create soak sample output") from exc
    try:
        mode = stat.S_IMODE(os.fstat(fd).st_mode)
        if mode & 0o077:
            raise SoakSamplingError("soak sample output permissions are too broad")
        return os.fdopen(fd, "w", encoding="utf-8", newline="\n")
    except Exception:
        os.close(fd)
        try:
            path.unlink()
        except OSError:
            pass
        raise


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument(
        "--compose-file",
        type=Path,
        default=repo_root / "docker-compose.poc.yml",
    )
    parser.add_argument(
        "--collector",
        type=Path,
        default=repo_root / "scripts" / "collect-soak-resource-sample.py",
    )
    parser.add_argument("--duration-seconds", type=float, required=True)
    parser.add_argument("--interval-seconds", type=float, required=True)
    parser.add_argument("--media-metrics-file", type=Path)
    parser.add_argument("--allow-unmeasured-media", action="store_true")
    parser.add_argument(
        "--collector-timeout-seconds",
        type=float,
        default=DEFAULT_COLLECTOR_TIMEOUT_SECONDS,
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        validate_project_name(args.project)
        duration = _finite_nonnegative(
            args.duration_seconds, "duration_seconds", positive=True
        )
        interval = _finite_nonnegative(
            args.interval_seconds, "interval_seconds", positive=True
        )
        timeout = _finite_nonnegative(
            args.collector_timeout_seconds,
            "collector_timeout_seconds",
            positive=True,
        )
        if args.media_metrics_file is None and not args.allow_unmeasured_media:
            raise SoakSamplingError(
                "--media-metrics-file is required unless --allow-unmeasured-media is explicit"
            )

        def collect(elapsed: float) -> dict[str, Any]:
            child_argv = build_collector_argv(
                collector_path=args.collector,
                project=args.project,
                compose_file=args.compose_file,
                elapsed_seconds=elapsed,
                media_metrics_file=args.media_metrics_file,
                allow_unmeasured_media=args.allow_unmeasured_media,
            )
            return collect_one_process(
                child_argv,
                expected_elapsed=elapsed,
                timeout_seconds=timeout,
            )

        with open_output_exclusive(args.output) as output:
            count = run_sampling(
                duration_seconds=duration,
                interval_seconds=interval,
                collect=collect,
                output=output,
            )
    except KeyboardInterrupt:
        print("soak sampling interrupted; completed JSONL samples were retained", file=sys.stderr)
        return 130
    except SoakSamplingError as exc:
        print(f"soak sampling failed: {exc}", file=sys.stderr)
        return 2

    print(f"soak sampling completed: samples={count} output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
