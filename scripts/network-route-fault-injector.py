#!/usr/bin/env python3
"""Plan or apply a bounded route-change fault for IRLight QA.

The helper only touches an explicitly named Linux network namespace. It never
creates namespaces, rewrites the default route, changes resolver configuration,
or edits host network state. ``plan`` is read-only. ``apply`` and ``clear``
require an explicit acknowledgement that the namespace is disposable.

The injected fault is a more-specific blackhole host route for one literal
IPv4/IPv6 destination. Existing exact host routes are detected before mutation
and cause a fail-closed refusal. The route installed by this helper is tagged
with a fixed protocol/metric pair so cleanup targets only the QA route.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Sequence

DURATION_SECONDS_CHOICES = (10, 30, 120, 600)
COMMAND_TIMEOUT_SECONDS = 10.0
ROUTE_PROTOCOL = "99"
ROUTE_METRIC = "42760"
_NAMESPACE_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class RouteFaultPlanError(ValueError):
    """Raised when a route-change request is unsafe or ambiguous."""


@dataclass(frozen=True)
class RouteFaultPlan:
    namespace: str
    destination: str
    prefix: str
    check_argv: tuple[str, ...]
    apply_argv: tuple[str, ...]
    cleanup_argv: tuple[str, ...]
    duration_seconds: int | None

    def as_dict(self) -> dict[str, object]:
        return {
            "namespace": self.namespace,
            "destination": self.destination,
            "prefix": self.prefix,
            "check_argv": list(self.check_argv),
            "apply_argv": list(self.apply_argv),
            "cleanup_argv": list(self.cleanup_argv),
            "duration_seconds": self.duration_seconds,
        }


def _validate_namespace(namespace: str) -> str:
    if (
        not isinstance(namespace, str)
        or not namespace
        or len(namespace.encode("utf-8")) > 63
        or namespace.startswith("-")
        or _NAMESPACE_RE.fullmatch(namespace) is None
    ):
        raise RouteFaultPlanError("namespace must be a canonical ip-netns name")
    return namespace


def _validate_destination(
    destination: str, *, allow_loopback: bool
) -> tuple[str, str, tuple[str, ...]]:
    if not isinstance(destination, str) or not destination:
        raise RouteFaultPlanError("destination must be a literal IP address")
    try:
        address = ipaddress.ip_address(destination)
    except ValueError:
        raise RouteFaultPlanError(
            "destination must be a literal IP address"
        ) from None
    if address.is_unspecified or address.is_multicast:
        raise RouteFaultPlanError("destination must be a unicast IP address")
    if address.is_loopback and allow_loopback is not True:
        raise RouteFaultPlanError(
            "loopback destination requires explicit allow_loopback acknowledgement"
        )
    prefix_length = 32 if address.version == 4 else 128
    family_args: tuple[str, ...] = () if address.version == 4 else ("-6",)
    return str(address), f"{address}/{prefix_length}", family_args


def _validate_duration(duration_seconds: int | None) -> int | None:
    if duration_seconds is None:
        return None
    if (
        isinstance(duration_seconds, bool)
        or not isinstance(duration_seconds, int)
        or duration_seconds not in DURATION_SECONDS_CHOICES
    ):
        raise RouteFaultPlanError("duration is outside the supported QA matrix")
    return duration_seconds


def build_route_fault_plan(
    *,
    namespace: str,
    destination: str,
    duration_seconds: int | None,
    allow_loopback: bool = False,
) -> RouteFaultPlan:
    """Build shell-free route commands scoped to one disposable namespace."""

    namespace = _validate_namespace(namespace)
    destination, prefix, family_args = _validate_destination(
        destination, allow_loopback=allow_loopback
    )
    duration_seconds = _validate_duration(duration_seconds)

    base = ("ip", "netns", "exec", namespace, "ip") + family_args
    check_argv = base + (
        "-json",
        "route",
        "show",
        "table",
        "main",
        "exact",
        prefix,
    )
    route_selector = (
        "blackhole",
        prefix,
        "table",
        "main",
        "proto",
        ROUTE_PROTOCOL,
        "metric",
        ROUTE_METRIC,
    )
    apply_argv = base + ("route", "add") + route_selector
    cleanup_argv = base + ("route", "del") + route_selector

    return RouteFaultPlan(
        namespace=namespace,
        destination=destination,
        prefix=prefix,
        check_argv=check_argv,
        apply_argv=apply_argv,
        cleanup_argv=cleanup_argv,
        duration_seconds=duration_seconds,
    )


def _run_capture(argv: Sequence[str]) -> tuple[int, str]:
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
        raise RuntimeError("route fault command could not complete safely") from exc
    return completed.returncode, completed.stdout


def _run(argv: Sequence[str]) -> None:
    returncode, _ = _run_capture(argv)
    if returncode != 0:
        raise RuntimeError(f"route fault command exited with status {returncode}")


def _exact_route_exists(argv: Sequence[str]) -> bool:
    returncode, stdout = _run_capture(argv)
    if returncode != 0:
        raise RuntimeError(
            f"route fault preflight check exited with status {returncode}"
        )
    try:
        routes = json.loads(stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError("route fault preflight returned invalid JSON") from exc
    if not isinstance(routes, list):
        raise RuntimeError("route fault preflight returned an unexpected payload")
    return bool(routes)


def apply_route_fault(
    *,
    namespace: str,
    destination: str,
    duration_seconds: int,
    confirm_disposable_namespace: bool,
    allow_loopback: bool = False,
) -> int:
    """Apply a bounded blackhole host route and always attempt exact cleanup."""

    if confirm_disposable_namespace is not True:
        raise RouteFaultPlanError(
            "disposable namespace acknowledgement is required"
        )
    plan = build_route_fault_plan(
        namespace=namespace,
        destination=destination,
        duration_seconds=duration_seconds,
        allow_loopback=allow_loopback,
    )
    assert plan.duration_seconds is not None

    if _exact_route_exists(plan.check_argv):
        raise RouteFaultPlanError(
            "an exact destination route already exists; refusing to replace it"
        )

    interrupted = False
    apply_error: Exception | None = None
    attempted_apply = False

    try:
        attempted_apply = True
        _run(plan.apply_argv)
        try:
            time.sleep(plan.duration_seconds)
        except KeyboardInterrupt:
            interrupted = True
    except KeyboardInterrupt:
        interrupted = True
    except Exception as exc:
        apply_error = exc

    cleanup_error: BaseException | None = None
    if attempted_apply:
        try:
            _run(plan.cleanup_argv)
        except (Exception, KeyboardInterrupt) as exc:
            cleanup_error = exc

    if cleanup_error is not None:
        if apply_error is not None:
            raise RuntimeError(
                "route fault apply failed and cleanup could not be confirmed"
            ) from cleanup_error
        raise RuntimeError("route fault cleanup failed") from cleanup_error
    if apply_error is not None:
        raise RuntimeError("route fault apply failed") from apply_error
    return 130 if interrupted else 0


def clear_route_fault(
    *,
    namespace: str,
    destination: str,
    confirm_disposable_namespace: bool,
    allow_loopback: bool = False,
) -> None:
    """Remove only the exact tagged QA blackhole route."""

    if confirm_disposable_namespace is not True:
        raise RouteFaultPlanError(
            "disposable namespace acknowledgement is required"
        )
    plan = build_route_fault_plan(
        namespace=namespace,
        destination=destination,
        duration_seconds=None,
        allow_loopback=allow_loopback,
    )
    _run(plan.cleanup_argv)


def _add_target_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--namespace", required=True, help="named disposable ip-netns"
    )
    parser.add_argument(
        "--destination",
        required=True,
        help="literal destination IPv4/IPv6 address; hostnames are rejected",
    )
    parser.add_argument(
        "--allow-loopback",
        action="store_true",
        help="explicitly allow a loopback destination inside the disposable namespace",
    )


def _add_execution_acknowledgement(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--confirm-disposable-namespace",
        action="store_true",
        required=True,
        help="acknowledge that route state in this namespace will be changed",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", help="print a read-only route-fault plan")
    _add_target_arguments(plan)
    plan.add_argument(
        "--duration",
        type=int,
        choices=DURATION_SECONDS_CHOICES,
        dest="duration_seconds",
    )
    plan.add_argument("--json", action="store_true", dest="json_output")

    apply_parser = subparsers.add_parser(
        "apply",
        help="apply a bounded route fault inside a disposable namespace",
    )
    _add_target_arguments(apply_parser)
    apply_parser.add_argument(
        "--duration",
        type=int,
        choices=DURATION_SECONDS_CHOICES,
        required=True,
        dest="duration_seconds",
    )
    _add_execution_acknowledgement(apply_parser)

    clear = subparsers.add_parser(
        "clear", help="remove the exact tagged route fault for a QA case"
    )
    _add_target_arguments(clear)
    _add_execution_acknowledgement(clear)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "plan":
            plan = build_route_fault_plan(
                namespace=args.namespace,
                destination=args.destination,
                duration_seconds=args.duration_seconds,
                allow_loopback=args.allow_loopback,
            )
            if args.json_output:
                print(json.dumps(plan.as_dict(), sort_keys=True))
            else:
                print(f"preflight: {shlex.join(plan.check_argv)}")
                print(f"apply:     {shlex.join(plan.apply_argv)}")
                print(f"cleanup:   {shlex.join(plan.cleanup_argv)}")
                if plan.duration_seconds is not None:
                    print(f"duration_seconds: {plan.duration_seconds}")
            return 0

        if args.command == "clear":
            clear_route_fault(
                namespace=args.namespace,
                destination=args.destination,
                confirm_disposable_namespace=args.confirm_disposable_namespace,
                allow_loopback=args.allow_loopback,
            )
            return 0

        assert args.command == "apply"
        return apply_route_fault(
            namespace=args.namespace,
            destination=args.destination,
            duration_seconds=args.duration_seconds,
            confirm_disposable_namespace=args.confirm_disposable_namespace,
            allow_loopback=args.allow_loopback,
        )
    except (RouteFaultPlanError, RuntimeError) as exc:
        print(f"network-route-fault-injector: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
