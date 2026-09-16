#!/usr/bin/env python3
"""Periodically collect fail-closed JSONL evidence from a disposable IRLight soak project.

This runner is deliberately read-only with respect to Docker. It invokes the fixed
``collect-soak-resource-sample.py`` helper on a monotonic schedule and persists each
successful sample as one canonical JSON object per line. Existing evidence files are
never overwritten; if collection fails or the process is interrupted, samples already
written remain available for an aborted/failure report.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, TextIO


class SoakOrchestrationError(RuntimeError):
    """Raised when periodic sample collection cannot continue safely."""


def positive_int(value: str) -> int:
    try:
        result = int(value, 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if result <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return result


def _reject_constant(value: str) -> None:
    raise SoakOrchestrationError(f"non-standard JSON numeric constant: {value}")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SoakOrchestrationError(f"duplicate JSON key from collector: {key}")
        result[key] = value
    return result


def parse_collector_output(raw: str) -> dict[str, Any]:
    if not raw.strip():
        raise SoakOrchestrationError("collector produced no JSON sample")
    try:
        value = json.loads(
            raw,
            parse_constant=_reject_constant,
            object_pairs_hook=_strict_object,
        )
    except json.JSONDecodeError as exc:
        raise SoakOrchestrationError(f"collector produced invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise SoakOrchestrationError("collector JSON sample must be an object")
    return value


def run_collector(
    *,
    collector: Path,
    project: str,
    compose_file: Path,
    elapsed_seconds: float,
    media_metrics_file: Path | None,
    allow_unmeasured_media: bool,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    if isinstance(elapsed_seconds, bool) or not isinstance(elapsed_seconds, (int, float)):
        raise SoakOrchestrationError("elapsed_seconds must be a finite non-negative number")
    elapsed = float(elapsed_seconds)
    if not math.isfinite(elapsed) or elapsed < 0:
        raise SoakOrchestrationError("elapsed_seconds must be a finite non-negative number")

    argv = [
        sys.executable,
        str(collector),
        "--project",
        project,
        "--compose-file",
        str(compose_file),
        "--elapsed-seconds",
        format(elapsed, ".9g"),
    ]
    if media_metrics_file is not None:
        argv.extend(["--media-metrics-file", str(media_metrics_file)])
    if allow_unmeasured_media:
        argv.append("--allow-unmeasured-media")

    try:
        completed = runner(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=60.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SoakOrchestrationError(f"collector failed to execute: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip()
        suffix = f": {detail}" if detail else ""
        raise SoakOrchestrationError(f"collector exited {completed.returncode}{suffix}")
    return parse_collector_output(completed.stdout)


def persist_sample(handle: TextIO, sample: dict[str, Any]) -> None:
    try:
        rendered = json.dumps(
            sample,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise SoakOrchestrationError(
            f"collector sample cannot be serialized safely: {exc}"
        ) from exc
    try:
        handle.write(rendered + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    except OSError as exc:
        raise SoakOrchestrationError(f"cannot persist soak sample: {exc}") from exc


def collect_on_schedule(
    *,
    duration_seconds: int,
    interval_seconds: int,
    emit: Callable[[float], None],
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    if (
        isinstance(duration_seconds, bool)
        or not isinstance(duration_seconds, int)
        or duration_seconds <= 0
    ):
        raise SoakOrchestrationError("duration_seconds must be a positive integer")
    if (
        isinstance(interval_seconds, bool)
        or not isinstance(interval_seconds, int)
        or interval_seconds <= 0
    ):
        raise SoakOrchestrationError("interval_seconds must be a positive integer")

    start = clock()
    emit(0.0)
    count = 1
    next_due = float(min(interval_seconds, duration_seconds))

    while True:
        elapsed = clock() - start
        if not math.isfinite(elapsed) or elapsed < 0:
            raise SoakOrchestrationError("monotonic clock returned an invalid elapsed time")
        if elapsed < next_due:
            sleeper(next_due - elapsed)

        elapsed = clock() - start
        if not math.isfinite(elapsed) or elapsed < 0:
            raise SoakOrchestrationError("monotonic clock returned an invalid elapsed time")
        # The scheduled deadline is a lower bound. Using max prevents tiny clock
        # precision effects from producing a final sample just below the target.
        observed = max(next_due, elapsed)
        emit(observed)
        count += 1
        if observed >= duration_seconds:
            break

        candidate = next_due + interval_seconds
        # A slow collector may span one or more scheduled slots. Do not fabricate
        # catch-up samples for times at which no observation occurred.
        while candidate <= observed:
            candidate += interval_seconds
        next_due = float(min(candidate, duration_seconds))

    return count


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project",
        required=True,
        help="disposable irlight-poc-soak-* Compose project",
    )
    parser.add_argument(
        "--compose-file",
        type=Path,
        default=repo_root / "docker-compose.poc.yml",
        help="Compose file used by the soak project",
    )
    parser.add_argument("--duration-seconds", required=True, type=positive_int)
    parser.add_argument("--interval-seconds", required=True, type=positive_int)
    parser.add_argument("--samples-jsonl", type=Path, required=True)
    parser.add_argument("--media-metrics-file", type=Path)
    parser.add_argument(
        "--allow-unmeasured-media",
        action="store_true",
        help="explicitly allow resource-only samples; not valid proof of media continuity",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    collector = Path(__file__).with_name("collect-soak-resource-sample.py")

    try:
        if not collector.is_file():
            raise SoakOrchestrationError(f"collector helper is missing: {collector}")
        try:
            if args.samples_jsonl.resolve() == args.compose_file.resolve():
                raise SoakOrchestrationError("samples JSONL must not overwrite the Compose file")
            if (
                args.media_metrics_file is not None
                and args.samples_jsonl.resolve() == args.media_metrics_file.resolve()
            ):
                raise SoakOrchestrationError(
                    "samples JSONL must not overwrite the media metrics file"
                )
        except OSError as exc:
            raise SoakOrchestrationError(f"cannot resolve evidence paths: {exc}") from exc

        # Exclusive creation protects prior evidence. Keep the file open for the
        # whole run so each fsync leaves a durable prefix if collection later fails.
        with args.samples_jsonl.open("x", encoding="utf-8") as handle:

            def emit(elapsed: float) -> None:
                sample = run_collector(
                    collector=collector,
                    project=args.project,
                    compose_file=args.compose_file,
                    elapsed_seconds=elapsed,
                    media_metrics_file=args.media_metrics_file,
                    allow_unmeasured_media=args.allow_unmeasured_media,
                )
                persist_sample(handle, sample)

            count = collect_on_schedule(
                duration_seconds=args.duration_seconds,
                interval_seconds=args.interval_seconds,
                emit=emit,
            )
    except KeyboardInterrupt:
        print(
            "soak sample orchestration interrupted; partial JSONL evidence was preserved",
            file=sys.stderr,
        )
        return 130
    except (SoakOrchestrationError, OSError, UnicodeError) as exc:
        print(f"soak sample orchestration failed: {exc}", file=sys.stderr)
        return 2

    print(
        f"soak sample collection complete: {count} samples over "
        f"{args.duration_seconds}s -> {args.samples_jsonl}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
