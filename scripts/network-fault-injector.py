#!/usr/bin/env python3
"""Plan or apply bounded Linux netem fault profiles for IRLight QA.

``plan`` is read-only: it prints the exact argv that would be executed. Every
mode still requires an explicitly named, disposable Linux network namespace so
the helper never generates or executes a host-interface ``tc`` command.
``apply`` additionally requires a bounded duration and an explicit disruption
acknowledgement. Applied qdiscs are removed in ``finally`` so an interrupted
test does not intentionally leave the requested fault behind.

This tool intentionally covers only qdisc-local packet loss, latency/jitter,
and complete packet blackholes. DNS, route, firewall, TCP reset, bandwidth,
and burst-loss injection remain separate test concerns because their cleanup
and blast radius differ.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Sequence

LOSS_PERCENT_CHOICES = (1, 3, 5, 10)
LATENCY_MS_CHOICES = (50, 100, 300, 1000)
DURATION_SECONDS_CHOICES = (10, 30, 120, 600)
_INTERFACE_RE = re.compile(r"^[A-Za-z0-9_.:@-]+$")
_NAMESPACE_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class FaultPlanError(ValueError):
    """Raised when a requested fault profile is unsafe or ambiguous."""


@dataclass(frozen=True)
class FaultPlan:
    interface: str
    namespace: str
    apply_argv: tuple[str, ...]
    cleanup_argv: tuple[str, ...]
    duration_seconds: int | None

    def as_dict(self) -> dict[str, object]:
        return {
            "interface": self.interface,
            "namespace": self.namespace,
            "apply_argv": list(self.apply_argv),
            "cleanup_argv": list(self.cleanup_argv),
            "duration_seconds": self.duration_seconds,
        }


def _validate_interface(interface: str, *, allow_loopback: bool) -> str:
    if (
        not interface
        or len(interface.encode("utf-8")) > 15
        or interface.startswith("-")
        or _INTERFACE_RE.fullmatch(interface) is None
    ):
        raise FaultPlanError("interface must be a canonical Linux interface name")
    if interface == "lo" and not allow_loopback:
        raise FaultPlanError("loopback requires --allow-loopback")
    return interface


def _validate_namespace(namespace: str) -> str:
    if (
        not namespace
        or len(namespace.encode("utf-8")) > 63
        or namespace.startswith("-")
        or _NAMESPACE_RE.fullmatch(namespace) is None
    ):
        raise FaultPlanError("namespace must be a canonical ip-netns name")
    return namespace


def build_fault_plan(
    *,
    interface: str,
    namespace: str,
    loss_percent: int | None,
    latency_ms: int | None,
    jitter_ms: int | None,
    disconnect: bool,
    duration_seconds: int | None,
    allow_loopback: bool = False,
) -> FaultPlan:
    """Build a namespaced, shell-free ``tc netem`` plan."""

    interface = _validate_interface(interface, allow_loopback=allow_loopback)
    namespace = _validate_namespace(namespace)

    if loss_percent is not None and loss_percent not in LOSS_PERCENT_CHOICES:
        raise FaultPlanError("loss_percent is outside the supported QA matrix")
    if latency_ms is not None and latency_ms not in LATENCY_MS_CHOICES:
        raise FaultPlanError("latency_ms is outside the supported QA matrix")
    if jitter_ms is not None:
        if latency_ms is None:
            raise FaultPlanError("jitter requires latency")
        if isinstance(jitter_ms, bool) or jitter_ms <= 0 or jitter_ms > latency_ms:
            raise FaultPlanError("jitter must be positive and no greater than latency")
    if disconnect and (
        loss_percent is not None or latency_ms is not None or jitter_ms is not None
    ):
        raise FaultPlanError("disconnect cannot be combined with other netem faults")
    if not disconnect and loss_percent is None and latency_ms is None:
        raise FaultPlanError("at least one fault must be selected")
    if duration_seconds is not None and duration_seconds not in DURATION_SECONDS_CHOICES:
        raise FaultPlanError("duration is outside the supported QA matrix")

    prefix = ["ip", "netns", "exec", namespace]
    argv = prefix + ["tc", "qdisc", "replace", "dev", interface, "root", "netem"]
    if disconnect:
        argv.extend(("loss", "100%"))
    else:
        if loss_percent is not None:
            argv.extend(("loss", f"{loss_percent}%"))
        if latency_ms is not None:
            argv.extend(("delay", f"{latency_ms}ms"))
            if jitter_ms is not None:
                argv.append(f"{jitter_ms}ms")

    cleanup = tuple(prefix + ["tc", "qdisc", "del", "dev", interface, "root"])
    return FaultPlan(
        interface=interface,
        namespace=namespace,
        apply_argv=tuple(argv),
        cleanup_argv=cleanup,
        duration_seconds=duration_seconds,
    )


def _run(argv: Sequence[str]) -> None:
    completed = subprocess.run(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "command failed"
        raise RuntimeError(f"{argv[0]} exited {completed.returncode}: {detail}")


def _add_profile_arguments(parser: argparse.ArgumentParser, *, duration_required: bool) -> None:
    parser.add_argument("--interface", required=True)
    parser.add_argument(
        "--namespace",
        required=True,
        help="named disposable ip-netns",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--loss", type=int, choices=LOSS_PERCENT_CHOICES, dest="loss_percent"
    )
    group.add_argument("--disconnect", action="store_true")
    parser.add_argument(
        "--latency", type=int, choices=LATENCY_MS_CHOICES, dest="latency_ms"
    )
    parser.add_argument("--jitter", type=int, dest="jitter_ms")
    parser.add_argument("--allow-loopback", action="store_true")
    parser.add_argument(
        "--duration",
        type=int,
        choices=DURATION_SECONDS_CHOICES,
        required=duration_required,
        dest="duration_seconds",
        help="bounded fault duration in seconds",
    )


def _add_execution_acknowledgement(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--confirm-disposable-namespace",
        action="store_true",
        required=True,
        help="acknowledge that root qdisc state inside the namespace may be replaced",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", help="print a read-only fault plan")
    _add_profile_arguments(plan, duration_required=False)
    plan.add_argument("--json", action="store_true", dest="json_output")

    apply_parser = subparsers.add_parser(
        "apply",
        help="apply a bounded fault inside a disposable named network namespace",
    )
    _add_profile_arguments(apply_parser, duration_required=True)
    _add_execution_acknowledgement(apply_parser)

    clear = subparsers.add_parser(
        "clear", help="remove the root qdisc inside a disposable named namespace"
    )
    clear.add_argument("--interface", required=True)
    clear.add_argument("--namespace", required=True)
    clear.add_argument("--allow-loopback", action="store_true")
    _add_execution_acknowledgement(clear)
    return parser


def _plan_from_args(args: argparse.Namespace) -> FaultPlan:
    return build_fault_plan(
        interface=args.interface,
        namespace=args.namespace,
        loss_percent=args.loss_percent,
        latency_ms=args.latency_ms,
        jitter_ms=args.jitter_ms,
        disconnect=args.disconnect,
        duration_seconds=args.duration_seconds,
        allow_loopback=args.allow_loopback,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "clear":
            interface = _validate_interface(
                args.interface, allow_loopback=args.allow_loopback
            )
            namespace = _validate_namespace(args.namespace)
            _run(
                (
                    "ip",
                    "netns",
                    "exec",
                    namespace,
                    "tc",
                    "qdisc",
                    "del",
                    "dev",
                    interface,
                    "root",
                )
            )
            return 0

        plan = _plan_from_args(args)
        if args.command == "plan":
            if args.json_output:
                print(json.dumps(plan.as_dict(), sort_keys=True))
            else:
                print(f"apply:   {shlex.join(plan.apply_argv)}")
                print(f"cleanup: {shlex.join(plan.cleanup_argv)}")
                if plan.duration_seconds is not None:
                    print(f"duration_seconds: {plan.duration_seconds}")
            return 0

        assert args.command == "apply"
        assert plan.duration_seconds is not None
        _run(plan.apply_argv)
        cleanup_error: Exception | None = None
        interrupted = False
        try:
            time.sleep(plan.duration_seconds)
        except KeyboardInterrupt:
            interrupted = True
        finally:
            try:
                _run(plan.cleanup_argv)
            except Exception as exc:  # cleanup failure must remain visible
                cleanup_error = exc
        if cleanup_error is not None:
            raise RuntimeError(
                f"network fault cleanup failed: {cleanup_error}"
            ) from cleanup_error
        return 130 if interrupted else 0
    except (FaultPlanError, RuntimeError) as exc:
        print(f"network-fault-injector: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
