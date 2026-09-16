#!/usr/bin/env python3
"""Plan or apply a bounded DNS-failure fault for IRLight QA.

The helper only touches an explicitly named Linux network namespace. It never
creates namespaces, changes routes, rewrites resolver configuration, resolves
hostnames, or edits host firewall state. ``plan`` is read-only. ``apply`` and
``clear`` require an explicit acknowledgement that the namespace is disposable.

The injected rules match one literal DNS resolver IP and destination port 53 in
the namespace OUTPUT chain. Both UDP and TCP DNS are rejected so resolver
fallback cannot silently turn a UDP-only fault into a successful lookup.
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
IPTABLES_WAIT_SECONDS = 2
_NAMESPACE_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_RULE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")


class DnsFaultPlanError(ValueError):
    """Raised when a DNS-failure request is unsafe or ambiguous."""


@dataclass(frozen=True)
class DnsFaultPlan:
    namespace: str
    resolver: str
    rule_id: str
    apply_argvs: tuple[tuple[str, ...], ...]
    cleanup_argvs: tuple[tuple[str, ...], ...]
    duration_seconds: int | None

    def as_dict(self) -> dict[str, object]:
        return {
            "namespace": self.namespace,
            "resolver": self.resolver,
            "rule_id": self.rule_id,
            "apply_argvs": [list(argv) for argv in self.apply_argvs],
            "cleanup_argvs": [list(argv) for argv in self.cleanup_argvs],
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
        raise DnsFaultPlanError("namespace must be a canonical ip-netns name")
    return namespace


def _validate_resolver(resolver: str) -> tuple[str, str]:
    if not isinstance(resolver, str) or not resolver:
        raise DnsFaultPlanError("resolver must be a literal IP address")
    try:
        address = ipaddress.ip_address(resolver)
    except ValueError:
        raise DnsFaultPlanError("resolver must be a literal IP address") from None
    if address.is_unspecified or address.is_multicast:
        raise DnsFaultPlanError("resolver must be a unicast IP address")
    command = "iptables" if address.version == 4 else "ip6tables"
    return str(address), command


def _validate_rule_id(rule_id: str) -> str:
    if not isinstance(rule_id, str) or _RULE_ID_RE.fullmatch(rule_id) is None:
        raise DnsFaultPlanError(
            "rule_id must be 1..32 characters using letters, digits, dot, underscore, or dash"
        )
    return rule_id


def _validate_duration(duration_seconds: int | None) -> int | None:
    if duration_seconds is None:
        return None
    if (
        isinstance(duration_seconds, bool)
        or not isinstance(duration_seconds, int)
        or duration_seconds not in DURATION_SECONDS_CHOICES
    ):
        raise DnsFaultPlanError("duration is outside the supported QA matrix")
    return duration_seconds


def build_dns_fault_plan(
    *,
    namespace: str,
    resolver: str,
    rule_id: str,
    duration_seconds: int | None,
) -> DnsFaultPlan:
    """Build shell-free firewall commands scoped to one disposable namespace."""

    namespace = _validate_namespace(namespace)
    resolver, firewall_command = _validate_resolver(resolver)
    rule_id = _validate_rule_id(rule_id)
    duration_seconds = _validate_duration(duration_seconds)

    prefix = (
        "ip",
        "netns",
        "exec",
        namespace,
        firewall_command,
        "-w",
        str(IPTABLES_WAIT_SECONDS),
    )
    apply_argvs: list[tuple[str, ...]] = []
    cleanup_argvs: list[tuple[str, ...]] = []
    for protocol in ("udp", "tcp"):
        rule = (
            "OUTPUT",
            "-p",
            protocol,
            "-d",
            resolver,
            "--dport",
            "53",
            "-m",
            "comment",
            "--comment",
            f"irlight-qa-dns-fault:{rule_id}:{protocol}",
            "-j",
            "REJECT",
        )
        apply_argvs.append(prefix + ("-I",) + rule[:1] + ("1",) + rule[1:])
        cleanup_argvs.append(prefix + ("-D",) + rule)

    return DnsFaultPlan(
        namespace=namespace,
        resolver=resolver,
        rule_id=rule_id,
        apply_argvs=tuple(apply_argvs),
        cleanup_argvs=tuple(cleanup_argvs),
        duration_seconds=duration_seconds,
    )


def _run(argv: Sequence[str]) -> None:
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
        raise RuntimeError("DNS fault command could not complete safely") from exc
    if completed.returncode != 0:
        raise RuntimeError(
            f"DNS fault command exited with status {completed.returncode}"
        )


def _cleanup_applied(
    cleanup_argvs: Sequence[tuple[str, ...]],
) -> BaseException | None:
    first_error: BaseException | None = None
    for argv in reversed(cleanup_argvs):
        try:
            _run(argv)
        except (Exception, KeyboardInterrupt) as exc:
            if first_error is None:
                first_error = exc
    return first_error


def apply_dns_fault(
    *,
    namespace: str,
    resolver: str,
    rule_id: str,
    duration_seconds: int,
    confirm_disposable_namespace: bool,
) -> int:
    """Apply bounded UDP+TCP DNS rejection and clean up exact attempted rules."""

    if confirm_disposable_namespace is not True:
        raise DnsFaultPlanError("disposable namespace acknowledgement is required")
    plan = build_dns_fault_plan(
        namespace=namespace,
        resolver=resolver,
        rule_id=rule_id,
        duration_seconds=duration_seconds,
    )
    assert plan.duration_seconds is not None

    attempted_cleanup_argvs: list[tuple[str, ...]] = []
    interrupted = False
    apply_error: Exception | None = None

    try:
        for apply_argv, cleanup_argv in zip(
            plan.apply_argvs, plan.cleanup_argvs, strict=True
        ):
            attempted_cleanup_argvs.append(cleanup_argv)
            _run(apply_argv)
        try:
            time.sleep(plan.duration_seconds)
        except KeyboardInterrupt:
            interrupted = True
    except KeyboardInterrupt:
        interrupted = True
    except Exception as exc:
        apply_error = exc

    cleanup_error = _cleanup_applied(attempted_cleanup_argvs)
    if cleanup_error is not None:
        if apply_error is not None:
            raise RuntimeError(
                "DNS fault apply failed and cleanup could not be confirmed"
            ) from cleanup_error
        raise RuntimeError("DNS fault cleanup failed") from cleanup_error
    if apply_error is not None:
        raise RuntimeError("DNS fault apply failed") from apply_error
    return 130 if interrupted else 0


def clear_dns_fault(
    *,
    namespace: str,
    resolver: str,
    rule_id: str,
    confirm_disposable_namespace: bool,
) -> None:
    """Remove both exact DNS fault rules for one QA case."""

    if confirm_disposable_namespace is not True:
        raise DnsFaultPlanError("disposable namespace acknowledgement is required")
    plan = build_dns_fault_plan(
        namespace=namespace,
        resolver=resolver,
        rule_id=rule_id,
        duration_seconds=None,
    )

    first_error: BaseException | None = None
    for argv in reversed(plan.cleanup_argvs):
        try:
            _run(argv)
        except (Exception, KeyboardInterrupt) as exc:
            if first_error is None:
                first_error = exc
    if first_error is not None:
        raise RuntimeError("DNS fault cleanup failed") from first_error


def _add_target_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--namespace", required=True, help="named disposable ip-netns")
    parser.add_argument(
        "--resolver",
        required=True,
        help="literal DNS resolver IPv4/IPv6 address; hostnames are rejected",
    )
    parser.add_argument(
        "--rule-id",
        required=True,
        help="unique QA case identifier used in firewall rule comments",
    )


def _add_execution_acknowledgement(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--confirm-disposable-namespace",
        action="store_true",
        required=True,
        help="acknowledge that firewall state in this namespace will be changed",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", help="print a read-only DNS-fault plan")
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
        help="apply a bounded DNS fault inside a disposable namespace",
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
        "clear", help="remove the exact DNS fault rules for a QA case"
    )
    _add_target_arguments(clear)
    _add_execution_acknowledgement(clear)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "plan":
            plan = build_dns_fault_plan(
                namespace=args.namespace,
                resolver=args.resolver,
                rule_id=args.rule_id,
                duration_seconds=args.duration_seconds,
            )
            if args.json_output:
                print(json.dumps(plan.as_dict(), sort_keys=True))
            else:
                for index, apply_argv in enumerate(plan.apply_argvs, start=1):
                    print(f"apply[{index}]:   {shlex.join(apply_argv)}")
                for index, cleanup_argv in enumerate(
                    reversed(plan.cleanup_argvs), start=1
                ):
                    print(f"cleanup[{index}]: {shlex.join(cleanup_argv)}")
                if plan.duration_seconds is not None:
                    print(f"duration_seconds: {plan.duration_seconds}")
            return 0

        if args.command == "clear":
            clear_dns_fault(
                namespace=args.namespace,
                resolver=args.resolver,
                rule_id=args.rule_id,
                confirm_disposable_namespace=args.confirm_disposable_namespace,
            )
            return 0

        assert args.command == "apply"
        return apply_dns_fault(
            namespace=args.namespace,
            resolver=args.resolver,
            rule_id=args.rule_id,
            duration_seconds=args.duration_seconds,
            confirm_disposable_namespace=args.confirm_disposable_namespace,
        )
    except (DnsFaultPlanError, RuntimeError) as exc:
        print(f"network-dns-fault-injector: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
