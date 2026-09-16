#!/usr/bin/env python3
"""Select or execute one deterministic IRLight network-fault matrix case.

``plan`` is read-only. ``apply`` executes exactly one case produced by
``network-fault-matrix.py`` inside an explicitly named disposable Linux network
namespace. The runner never creates a namespace, never targets a host interface,
and always attempts qdisc cleanup after an apply attempt.
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
    bandwidth_kbits: Sequence[int] = (),
    allow_loopback: bool = False,
) -> dict[str, object]:
    """Return one exact case from the deterministic matrix."""

    matrix = MATRIX.build_matrix(
        namespace=namespace,
        interface=interface,
        protocols=MATRIX.PROTOCOL_CHOICES,
        profile_duration_seconds=profile_duration_seconds,
        bandwidth_kbits=bandwidth_kbits,
        allow_loopback=allow_loopback,
    )
    for case in matrix["cases"]:
        if case["id"] == case_id:
            return case
    raise CaseRunnerError("requested network fault case is not in the matrix")


def _validated_generated_case(
    case: dict[str, object], *, allow_loopback: bool
) -> dict[str, object]:
    """Regenerate and compare a case before any command is executed.

    ``execute_case`` is intentionally usable by Python test harnesses as well as
    the CLI, so it must not trust argv copied from an arbitrary/deserialized
    mapping. Only the exact closed-shape case emitted by the deterministic matrix
    is executable.
    """

    case_id = case.get("id")
    protocol = case.get("protocol")
    plan = case.get("plan")
    fault = case.get("fault")
    if (
        not isinstance(case_id, str)
        or not case_id
        or protocol not in MATRIX.PROTOCOL_CHOICES
        or not isinstance(plan, dict)
        or not isinstance(fault, dict)
    ):
        raise CaseRunnerError("network fault case has invalid execution data")

    namespace = plan.get("namespace")
    interface = plan.get("interface")
    duration_seconds = fault.get("duration_seconds")
    disconnect = fault.get("disconnect")
    bandwidth_kbit = fault.get("bandwidth_kbit")
    if (
        not isinstance(namespace, str)
        or not namespace
        or not isinstance(interface, str)
        or not interface
        or isinstance(duration_seconds, bool)
        or not isinstance(duration_seconds, int)
        or duration_seconds not in MATRIX.INJECTOR.DURATION_SECONDS_CHOICES
        or not isinstance(disconnect, bool)
        or (
            bandwidth_kbit is not None
            and (
                isinstance(bandwidth_kbit, bool)
                or not isinstance(bandwidth_kbit, int)
                or bandwidth_kbit < MATRIX.INJECTOR.BANDWIDTH_KBIT_MIN
                or bandwidth_kbit > MATRIX.INJECTOR.BANDWIDTH_KBIT_MAX
            )
        )
    ):
        raise CaseRunnerError("network fault case has invalid execution data")

    profile_duration_seconds = (
        MATRIX.DEFAULT_PROFILE_DURATION_SECONDS if disconnect else duration_seconds
    )
    bandwidth_kbits = () if bandwidth_kbit is None else (bandwidth_kbit,)
    try:
        expected = select_case(
            case_id=case_id,
            namespace=namespace,
            interface=interface,
            profile_duration_seconds=profile_duration_seconds,
            bandwidth_kbits=bandwidth_kbits,
            allow_loopback=allow_loopback,
        )
    except (CaseRunnerError, MATRIX.MatrixError, MATRIX.INJECTOR.FaultPlanError) as exc:
        raise CaseRunnerError(
            "network fault case could not be regenerated safely"
        ) from exc

    if case != expected:
        raise CaseRunnerError("network fault case does not match generated matrix")
    return expected


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


def execute_case(
    case: dict[str, object],
    *,
    confirm_disposable_namespace: bool = False,
    allow_loopback: bool = False,
) -> int:
    """Apply one generated matrix case, wait its bounded duration, then clean up."""

    if confirm_disposable_namespace is not True:
        raise CaseRunnerError("disposable namespace acknowledgement is required")

    case = _validated_generated_case(case, allow_loopback=allow_loopback)
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

    interrupted = False
    apply_error: Exception | None = None
    cleanup_error: BaseException | None = None
    try:
        _run_command(apply_argv)
        try:
            time.sleep(duration_seconds)
        except KeyboardInterrupt:
            interrupted = True
    except KeyboardInterrupt:
        interrupted = True
    except Exception as exc:
        apply_error = exc
    finally:
        try:
            _run_command(cleanup_argv)
        except (Exception, KeyboardInterrupt) as exc:  # cleanup failure must remain visible
            cleanup_error = exc

    if cleanup_error is not None:
        if apply_error is not None:
            raise CaseRunnerError(
                "network fault apply failed and cleanup could not be confirmed"
            ) from cleanup_error
        raise CaseRunnerError("network fault cleanup failed") from cleanup_error
    if apply_error is not None:
        raise CaseRunnerError("network fault apply failed") from apply_error
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
        help="duration used by loss, latency, and explicit bandwidth case IDs",
    )
    parser.add_argument(
        "--bandwidth-kbit",
        type=int,
        action="append",
        dest="bandwidth_kbits",
        metavar="KBIT",
        help="explicit bandwidth value used to generate/select bandwidth case IDs",
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
            bandwidth_kbits=args.bandwidth_kbits or (),
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
        return execute_case(
            case,
            confirm_disposable_namespace=args.confirm_disposable_namespace,
            allow_loopback=args.allow_loopback,
        )
    except (CaseRunnerError, MATRIX.MatrixError, MATRIX.INJECTOR.FaultPlanError) as exc:
        print(f"network-fault-case-runner: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
