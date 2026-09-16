#!/usr/bin/env python3
"""Select or execute one deterministic IRLight network-fault matrix case.

``plan`` is read-only. ``apply`` executes exactly one case produced by
``network-fault-matrix.py`` inside an explicitly named disposable Linux network
namespace. The runner never creates a namespace, never targets a host interface,
and always attempts qdisc cleanup after a successfully applied fault.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Sequence

COMMAND_TIMEOUT_SECONDS = 10.0


class CaseRunnerError(RuntimeError):
    """Raised when a matrix case cannot be selected or safely executed."""


def _load_matrix() -> ModuleType:
    path = Path(__file__).resolve().with_name("network-fault-matrix.py")
    spec = importlib.util.spec_from_file_location("irlight_network_fault_matrix", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("network fault matrix module cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MATRIX = _load_matrix()


def select_case(
    *,
    case_id: str,
    namespace: str,
    interface: str,
    profile_duration_seconds: int = MATRIX.DEFAULT_PROFILE_DURATION_SECONDS,
    allow_loopback: bool = False,
) -> dict[str, object]:
    """Return one exact case from the deterministic baseline matrix."""

    matrix = MATRIX.build_matrix(
        namespace=namespace,
        interface=interface,
        protocols=MATRIX.PROTOCOL_CHOICES,
        profile_duration_seconds=profile_duration_seconds,
        allow_loopback=allow_loopback,
    )
    for case in matrix["cases"]:
        if case["id"] == case_id:
            return case
    raise CaseRunnerError("requested network fault case is not in the matrix")


def _run_command(argv: Sequence[str]) -> None:
    try:
        completed = subprocess.run(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CaseRunnerError("network fault command could not complete safely") from exc
    if completed.returncode != 0:
        raise CaseRunnerError(
            f"network fault command exited with status {completed.returncode}"
        )


def execute_case(case: dict[str, object]) -> int:
    """Apply one generated matrix case, wait its bounded duration, then clean up."""

    plan = case.get("plan")
    fault = case.get("fault")
    if not isinstance(plan, dict) or not isinstance(fault, dict):
        raise CaseRunnerError("network fault case has invalid structure")

    apply_argv = plan.get("apply_argv")
    cleanup_argv = plan.get("cleanup_argv")
    duration_seconds = fault.get("duration_seconds")
    if (
        not isinstance(apply_argv, list)
        or not apply_argv
        or not all(isinstance(value, str) and value for value in apply_argv)
        or not isinstance(cleanup_argv, list)
        or not cleanup_argv
        or not all(isinstance(value, str) and value for value in cleanup_argv)
        or isinstance(duration_seconds, bool)
        or not isinstance(duration_seconds, int)
        or duration_seconds not in MATRIX.INJECTOR.DURATION_SECONDS_CHOICES
    ):
        raise CaseRunnerError("network fault case has invalid execution data")

    _run_command(apply_argv)
    interrupted = False
    cleanup_error: Exception | None = None
    try:
        time.sleep(duration_seconds)
    except KeyboardInterrupt:
        interrupted = True
    finally:
        try:
            _run_command(cleanup_argv)
        except Exception as exc:  # cleanup failure must remain visible
            cleanup_error = exc

    if cleanup_error is not None:
        raise CaseRunnerError("network fault cleanup failed") from cleanup_error
    return 130 if interrupted else 0


def _add_case_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--case", required=True, dest="case_id")
    parser.add_argument("--namespace", required=True, help="named disposable ip-netns")
    parser.add_argument("--interface", required=True)
    parser.add_argument(
        "--duration",
        type=int,
        choices=MATRIX.INJECTOR.DURATION_SECONDS_CHOICES,
        default=MATRIX.DEFAULT_PROFILE_DURATION_SECONDS,
        dest="profile_duration_seconds",
        help="duration used by loss and latency case IDs",
    )
    parser.add_argument("--allow-loopback", action="store_true")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", help="print one selected case without execution")
    _add_case_arguments(plan)
    plan.add_argument("--pretty", action="store_true")

    apply_parser = subparsers.add_parser(
        "apply", help="execute one case inside a disposable named network namespace"
    )
    _add_case_arguments(apply_parser)
    apply_parser.add_argument(
        "--confirm-disposable-namespace",
        action="store_true",
        required=True,
        help="acknowledge that root qdisc state inside the namespace may be replaced",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        case = select_case(
            case_id=args.case_id,
            namespace=args.namespace,
            interface=args.interface,
            profile_duration_seconds=args.profile_duration_seconds,
            allow_loopback=args.allow_loopback,
        )
        if args.command == "plan":
            print(
                json.dumps(
                    case,
                    ensure_ascii=False,
                    indent=2 if args.pretty else None,
                    sort_keys=True,
                )
            )
            return 0
        return execute_case(case)
    except (CaseRunnerError, MATRIX.MatrixError, MATRIX.INJECTOR.FaultPlanError) as exc:
        print(f"network-fault-case-runner: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
