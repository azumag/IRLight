#!/usr/bin/env python3
"""Run an isolated Compose soak and assemble machine-readable evidence.

This wrapper connects the existing disposable Compose soak runner, periodic
sample collector, strict report assembler, and report validator without
weakening any of their individual safety boundaries.  A successful wrapper
exit means the soak completed, project-scoped cleanup was verified by
``soak-compose.sh``, the schema-v1 report was assembled, and the canonical
validator accepted it.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class MeasuredSoakError(ValueError):
    """Raised when a measured soak cannot be run or evidenced safely."""


@dataclass(frozen=True)
class Paths:
    output_dir: Path
    samples: Path
    report: Path
    summary: Path
    run_result: Path


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--duration-seconds", type=_positive_int, default=600)
    parser.add_argument("--interval-seconds", type=_positive_int, default=30)
    parser.add_argument("--notes", default="")
    media = parser.add_mutually_exclusive_group(required=True)
    media.add_argument("--media-metrics-file", type=Path)
    media.add_argument("--allow-unmeasured-media", action="store_true")
    return parser.parse_args(argv)


def _validate_text(value: str, *, label: str, maximum: int) -> str:
    if not value.strip():
        raise MeasuredSoakError(f"{label} must be non-empty")
    if len(value) > maximum:
        raise MeasuredSoakError(f"{label} must be at most {maximum} characters")
    return value


def _prepare_paths(output_dir: Path) -> Paths:
    try:
        output_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
    except FileExistsError as exc:
        raise MeasuredSoakError("output directory already exists; refusing to overwrite evidence") from exc
    except OSError as exc:
        raise MeasuredSoakError(f"cannot create output directory: {exc}") from exc
    return Paths(
        output_dir=output_dir,
        samples=output_dir / "samples.jsonl",
        report=output_dir / "report.json",
        summary=output_dir / "summary.json",
        run_result=output_dir / "run-result.json",
    )


def _write_json_exclusive(path: Path, value: dict[str, Any]) -> None:
    rendered = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
        separators=(",", ":"),
    ) + "\n"
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
    except (OSError, TypeError, ValueError) as exc:
        raise MeasuredSoakError(f"cannot write {path.name}: {exc}") from exc


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _signal_process_group(process: subprocess.Popen[Any], sig: signal.Signals) -> None:
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass


def _run_soak(*, repo_root: Path, env: dict[str, str]) -> int:
    """Run the soak in its own process group so Ctrl-C can trigger shell cleanup.

    ``subprocess.run`` kills its direct child when ``KeyboardInterrupt`` escapes,
    which can prevent the shell's EXIT trap from running.  Keep ownership of the
    process instead: forward SIGINT to the whole child group, allow the shell a
    bounded cleanup window, and always return a non-zero interrupted status.
    """
    try:
        process = subprocess.Popen(
            ["bash", str(repo_root / "scripts" / "soak-compose.sh")],
            cwd=repo_root,
            env=env,
            text=True,
            start_new_session=True,
        )
    except OSError as exc:
        raise MeasuredSoakError(f"cannot start soak runner: {exc}") from exc

    try:
        return int(process.wait())
    except KeyboardInterrupt:
        _signal_process_group(process, signal.SIGINT)
        try:
            process.wait(timeout=120)
        except subprocess.TimeoutExpired:
            _signal_process_group(process, signal.SIGTERM)
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                _signal_process_group(process, signal.SIGKILL)
                process.wait()
        except KeyboardInterrupt:
            _signal_process_group(process, signal.SIGKILL)
            process.wait()
        return 130


def _run_assembler(
    *,
    repo_root: Path,
    paths: Paths,
    run_id: str,
    scenario: str,
    duration_seconds: int,
    soak_exit_code: int,
    notes: str,
) -> subprocess.CompletedProcess[str]:
    passed = soak_exit_code == 0
    cleanup_details = (
        "soak-compose exited 0 after its project-scoped cleanup verifier succeeded"
        if passed
        else (
            f"soak-compose exited {soak_exit_code}; cleanup is conservatively unverified "
            "because a failing runner exit does not distinguish soak failure from cleanup failure"
        )
    )
    command = [
        sys.executable,
        str(repo_root / "scripts" / "assemble-soak-report.py"),
        "--samples-jsonl",
        str(paths.samples),
        "--run-id",
        run_id,
        "--scenario",
        scenario,
        "--target-duration-seconds",
        str(duration_seconds),
        "--outcome",
        "pass" if passed else "fail",
        "--cleanup-details",
        cleanup_details,
        "--notes",
        notes,
        "--output",
        str(paths.report),
    ]
    if passed:
        command.append("--cleanup-verified")
    return subprocess.run(command, cwd=repo_root, text=True, check=False)


def _run_validator(*, repo_root: Path, paths: Paths) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts" / "validate-soak-report.py"),
            str(paths.report),
            "--json",
        ],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )


def run(args: argparse.Namespace) -> int:
    scenario = _validate_text(args.scenario, label="scenario", maximum=200)
    if len(args.notes) > 4000:
        raise MeasuredSoakError("notes must be at most 4000 characters")
    if args.interval_seconds > args.duration_seconds:
        raise MeasuredSoakError("interval-seconds must not exceed duration-seconds")

    media_metrics: Path | None = args.media_metrics_file
    if media_metrics is not None:
        try:
            media_metrics = media_metrics.resolve(strict=True)
        except OSError as exc:
            raise MeasuredSoakError(f"cannot resolve media metrics file: {exc}") from exc
        if not media_metrics.is_file():
            raise MeasuredSoakError("media metrics path must be a regular file")

    try:
        output_dir = args.output_dir.resolve()
    except OSError as exc:
        raise MeasuredSoakError(f"cannot resolve output directory: {exc}") from exc
    paths = _prepare_paths(output_dir)
    repo_root = _repo_root()
    run_id = str(uuid.uuid4())
    env = os.environ.copy()
    env.update(
        {
            "SOAK_SECONDS": str(args.duration_seconds),
            "SOAK_INTERVAL_SECONDS": str(args.interval_seconds),
            "SOAK_SAMPLES_JSONL": str(paths.samples),
        }
    )
    if media_metrics is not None:
        env["SOAK_MEDIA_METRICS_FILE"] = str(media_metrics)
        env.pop("SOAK_ALLOW_UNMEASURED_MEDIA", None)
        media_mode = "measured"
    else:
        env.pop("SOAK_MEDIA_METRICS_FILE", None)
        env["SOAK_ALLOW_UNMEASURED_MEDIA"] = "1"
        media_mode = "diagnostic-unmeasured"

    soak_exit_code = _run_soak(repo_root=repo_root, env=env)
    cleanup_verified = soak_exit_code == 0
    report_created = False
    summary_created = False
    evidence_error: str | None = None

    if paths.samples.is_file() and paths.samples.stat().st_size > 0:
        assembly = _run_assembler(
            repo_root=repo_root,
            paths=paths,
            run_id=run_id,
            scenario=scenario,
            duration_seconds=args.duration_seconds,
            soak_exit_code=soak_exit_code,
            notes=args.notes,
        )
        if assembly.returncode == 0:
            report_created = True
            validation = _run_validator(repo_root=repo_root, paths=paths)
            if validation.returncode == 0:
                try:
                    summary = json.loads(validation.stdout)
                except (json.JSONDecodeError, TypeError) as exc:
                    evidence_error = f"validator returned invalid JSON summary: {exc}"
                else:
                    if not isinstance(summary, dict):
                        evidence_error = "validator JSON summary must be an object"
                    else:
                        _write_json_exclusive(paths.summary, summary)
                        summary_created = True
            else:
                evidence_error = (
                    validation.stderr.strip()
                    or f"report validator exited {validation.returncode}"
                )
        else:
            evidence_error = f"report assembler exited {assembly.returncode}"
    else:
        evidence_error = "no sample evidence was produced"

    _write_json_exclusive(
        paths.run_result,
        {
            "schema_version": 1,
            "run_id": run_id,
            "scenario": scenario,
            "target_duration_seconds": args.duration_seconds,
            "sample_interval_seconds": args.interval_seconds,
            "media_mode": media_mode,
            "soak_exit_code": soak_exit_code,
            "cleanup_verified": cleanup_verified,
            "samples_file": paths.samples.name if paths.samples.exists() else None,
            "report_file": paths.report.name if report_created else None,
            "summary_file": paths.summary.name if summary_created else None,
            "evidence_error": evidence_error,
        },
    )

    if soak_exit_code == 0 and (not report_created or not summary_created):
        raise MeasuredSoakError(
            evidence_error or "successful soak did not produce validated evidence"
        )
    return soak_exit_code


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except MeasuredSoakError as exc:
        print(f"measured soak failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
