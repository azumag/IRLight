#!/usr/bin/env python3
"""Run one canonical Node-capacity scenario through an explicit local harness.

The runner deliberately does not choose acceptance thresholds or a safety margin.
An operator-provided harness classifies each measured level as pass/fail. This
orchestrator validates the canonical Issue #13 plan, executes its concurrency
ladder in order, stops after the first failing level, and records each result
through the existing strict raw-trial recorder.

Harness contract
----------------
The command supplied with --runner/--runner-arg receives two final positional
arguments: REQUEST_JSON RESULT_JSON. REQUEST_JSON is a schema-v1 object with
profile_label, scenario_id, and concurrent_sessions. The harness must create
RESULT_JSON containing exactly the measured trial fields except
concurrent_sessions, which is pinned by the canonical plan.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import signal
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any


MAX_RESULT_BYTES = 64 * 1024
RESULT_FIELDS = {
    "duration_seconds",
    "outcome",
    "cpu_peak_percent",
    "memory_rss_peak_bytes",
    "egress_peak_bps",
    "failed_sessions",
    "unexpected_reconnects",
}


class ScenarioRunError(ValueError):
    """Raised when a planned load scenario cannot be executed safely."""


def _load_script(filename: str, module_name: str) -> ModuleType:
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ScenarioRunError(f"cannot load {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # pragma: no cover - repository packaging failure
        raise ScenarioRunError(f"cannot load {filename}") from exc
    return module


def _reject_constant(value: str) -> None:
    raise ScenarioRunError(f"non-standard JSON numeric constant: {value}")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ScenarioRunError(f"duplicate JSON key: {key}")
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


def _read_result_bytes(path: Path) -> bytes:
    """Read one bounded stable regular result without following a final symlink."""

    try:
        before = os.lstat(path)
    except OSError as exc:
        raise ScenarioRunError("runner did not create a readable result") from exc
    if not stat.S_ISREG(before.st_mode):
        raise ScenarioRunError("runner result must be a regular file")
    if before.st_size > MAX_RESULT_BYTES:
        raise ScenarioRunError("runner result exceeds maximum size")

    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ScenarioRunError("cannot open runner result") from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise ScenarioRunError("runner result must be a regular file")
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise ScenarioRunError("runner result changed while opening")
        if opened.st_size > MAX_RESULT_BYTES:
            raise ScenarioRunError("runner result exceeds maximum size")

        chunks: list[bytes] = []
        remaining = MAX_RESULT_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_RESULT_BYTES:
            raise ScenarioRunError("runner result exceeds maximum size")

        after_read = os.fstat(fd)
        try:
            after_path = os.lstat(path)
        except OSError as exc:
            raise ScenarioRunError("runner result changed while reading") from exc
        if not stat.S_ISREG(after_path.st_mode):
            raise ScenarioRunError("runner result changed while reading")
        if _identity(opened) != _identity(after_read) or _identity(opened) != _identity(after_path):
            raise ScenarioRunError("runner result changed while reading")
        return raw
    finally:
        os.close(fd)


def _parse_result(path: Path) -> dict[str, Any]:
    try:
        raw = _read_result_bytes(path).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ScenarioRunError("runner result must be valid UTF-8") from exc
    try:
        value = json.loads(raw, parse_constant=_reject_constant, object_pairs_hook=_strict_object)
    except json.JSONDecodeError as exc:
        raise ScenarioRunError("runner result must be valid JSON") from exc
    except RecursionError as exc:
        raise ScenarioRunError("runner result JSON nesting is too deep") from exc
    if not isinstance(value, dict):
        raise ScenarioRunError("runner result root must be an object")
    if set(value) != RESULT_FIELDS:
        raise ScenarioRunError("runner result fields do not match the harness contract")
    return value


def _write_request(path: Path, request: dict[str, Any]) -> None:
    rendered = (
        json.dumps(
            request,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags, 0o600)
    try:
        view = memoryview(rendered)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise ScenarioRunError("cannot write harness request")
            view = view[written:]
        os.fsync(fd)
    except Exception:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    finally:
        os.close(fd)


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, AttributeError):
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _invoke_harness(
    command: list[str],
    request: dict[str, Any],
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="irlight-node-capacity-") as directory_name:
        directory = Path(directory_name)
        request_path = directory / "request.json"
        result_path = directory / "result.json"
        _write_request(request_path, request)

        argv = [*command, str(request_path), str(result_path)]
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            raise ScenarioRunError("cannot start capacity harness") from exc
        try:
            return_code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            _kill_process_group(process)
            raise ScenarioRunError("capacity harness exceeded the requested timeout") from exc
        if return_code != 0:
            raise ScenarioRunError("capacity harness exited unsuccessfully")
        return _parse_result(result_path)


def _positive_finite_float(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return value


def run_scenario(
    *,
    plan_path: Path,
    scenario_id: str,
    trials_jsonl: Path,
    runner_command: list[str],
    timeout_seconds: float,
) -> dict[str, Any]:
    if not runner_command or not runner_command[0]:
        raise ScenarioRunError("runner command must not be empty")
    if trials_jsonl.exists() or trials_jsonl.is_symlink():
        raise ScenarioRunError("trials JSONL already exists; use a new evidence path")
    if not trials_jsonl.parent.exists():
        raise ScenarioRunError("trials JSONL parent directory does not exist")

    validator = _load_script(
        "validate-node-capacity-load-plan.py",
        "irlight_node_capacity_load_plan_validator_for_runner",
    )
    recorder = _load_script(
        "record-node-capacity-trial.py",
        "irlight_node_capacity_trial_recorder_for_runner",
    )
    try:
        plan = validator.load_plan(plan_path)
        validator.validate_plan(plan)
    except validator.PlanValidationError as exc:
        raise ScenarioRunError("load plan is not canonical") from exc

    scenarios = {
        scenario["id"]: scenario
        for scenario in plan["scenarios"]
        if isinstance(scenario, dict) and isinstance(scenario.get("id"), str)
    }
    scenario = scenarios.get(scenario_id)
    if scenario is None:
        raise ScenarioRunError("scenario is not present in the canonical load plan")

    recorded_levels: list[int] = []
    boundary_found = False
    for concurrent_sessions in scenario["session_counts"]:
        request = {
            "schema_version": 1,
            "profile_label": plan["profile_label"],
            "scenario_id": scenario_id,
            "concurrent_sessions": concurrent_sessions,
        }
        measured = _invoke_harness(
            runner_command,
            request,
            timeout_seconds=timeout_seconds,
        )
        trial = {"concurrent_sessions": concurrent_sessions, **measured}
        try:
            recorded = recorder.append_trial(trials_jsonl, trial)
        except recorder.CapacityTrialRecordError as exc:
            raise ScenarioRunError("capacity harness returned an invalid trial") from exc
        recorded_levels.append(recorded["concurrent_sessions"])
        if recorded["outcome"] == "fail":
            boundary_found = True
            break

    return {
        "schema_version": 1,
        "scenario_id": scenario_id,
        "tested_load_levels": recorded_levels,
        "boundary_found": boundary_found,
        "stopped_after_first_failure": boundary_found,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--trials-jsonl", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=_positive_finite_float, required=True)
    parser.add_argument("--runner", required=True, help="Local harness executable.")
    parser.add_argument(
        "--runner-arg",
        action="append",
        default=[],
        help="Argument placed before the harness request/result paths. May be repeated.",
    )
    parser.add_argument("--json", action="store_true", help="Emit a deterministic JSON summary.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        summary = run_scenario(
            plan_path=args.plan,
            scenario_id=args.scenario,
            trials_jsonl=args.trials_jsonl,
            runner_command=[args.runner, *args.runner_arg],
            timeout_seconds=args.timeout_seconds,
        )
    except (ScenarioRunError, OSError, UnicodeError) as exc:
        print(f"node capacity scenario run failed: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, allow_nan=False))
    else:
        levels = ",".join(str(value) for value in summary["tested_load_levels"])
        print(
            "node capacity scenario run complete: "
            f"scenario={summary['scenario_id']} levels={levels} "
            f"boundary_found={'yes' if summary['boundary_found'] else 'no'}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
