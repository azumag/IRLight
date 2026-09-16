#!/usr/bin/env python3
"""Plan or apply a bounded TCP-reset fault for IRLight QA.

The helper only touches an explicitly named Linux network namespace. It never
creates namespaces, changes routes, resolves hostnames, or edits host firewall
state. ``plan`` is read-only. ``apply`` and ``clear`` require an explicit
acknowledgement that the namespace is disposable.

The injected rule matches one literal destination IP and one TCP destination
port in the namespace OUTPUT chain and rejects matching traffic with a TCP RST.
This is intended for protocol-specific RTMP/RTMPS disconnect recovery testing;
SRT is UDP and is intentionally outside this helper's scope.
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


class TcpResetPlanError(ValueError):
    """Raised when a TCP-reset request is unsafe or ambiguous."""


@dataclass(frozen=True)
class TcpResetPlan:
    namespace: str
    destination: str
    port: int
    rule_id: str
    apply_argv: tuple[str, ...]
    cleanup_argv: tuple[str, ...]
    duration_seconds: int | None

    def as_dict(self) -> dict[str, object]:
        return {
            "namespace": self.namespace,
            "destination": self.destination,
            "port": self.port,
            "rule_id": self.rule_id,
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
        raise TcpResetPlanError("namespace must be a canonical ip-netns name")
    return namespace


def _validate_destination(destination: str) -> tuple[str, str]:
    if not isinstance(destination, str) or not destination:
        raise TcpResetPlanError("destination must be a literal IP address")
    try:
        address = ipaddress.ip_address(destination)
    except ValueError:
        raise TcpResetPlanError("destination must be a literal IP address") from None
    if address.is_unspecified or address.is_multicast:
        raise TcpResetPlanError("destination must be a unicast IP address")
    command = "iptables" if address.version == 4 else "ip6tables"
    return str(address), command


def _validate_port(port: int) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise TcpResetPlanError("port must be an integer between 1 and 65535")
    return port


def _validate_rule_id(rule_id: str) -> str:
    if not isinstance(rule_id, str) or _RULE_ID_RE.fullmatch(rule_id) is None:
        raise TcpResetPlanError(
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
        raise TcpResetPlanError("duration is outside the supported QA matrix")
    return duration_seconds


def build_tcp_reset_plan(
    *,
    namespace: str,
    destination: str,
    port: int,
    rule_id: str,
    duration_seconds: int | None,
) -> TcpResetPlan:
    """Build a shell-free firewall plan scoped to one disposable namespace."""

    namespace = _validate_namespace(namespace)
    destination, firewall_command = _validate_destination(destination)
    port = _validate_port(port)
    rule_id = _validate_rule_id(rule_id)
    duration_seconds = _validate_duration(duration_seconds)

    prefix = ("ip", "netns", "exec", namespace, firewall_command, "-w", str(IPTABLES_WAIT_SECONDS))
    rule = (
        "OUTPUT",
        "-p",
        "tcp",
        "-d",
        destination,
        "--dport",
        str(port),
        "-m",
        "comment",
        "--comment",
        f"irlight-qa-tcp-reset:{rule_id}",
        "-j",
        "REJECT",
        "--reject-with",
        "tcp-reset",
    )
    apply_argv = prefix + ("-I",) + rule[:1] + ("1",) + rule[1:]
    cleanup_argv = prefix + ("-D",) + rule
    return TcpResetPlan(
        namespace=namespace,
        destination=destination,
        port=port,
        rule_id=rule_id,
        apply_argv=apply_argv,
        cleanup_argv=cleanup_argv,
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
        raise RuntimeError("TCP reset command could not complete safely") from exc
    if completed.returncode != 0:
        raise RuntimeError(
            f"TCP reset command exited with status {completed.returncode}"
        )


def apply_tcp_reset(
    *,
    namespace: str,
    destination: str,
    port: int,
    rule_id: str,
    duration_seconds: int,
    confirm_disposable_namespace: bool,
) -> int:
    """Apply one bounded reset rule and always attempt exact cleanup."""

    if confirm_disposable_namespace is not True:
        raise TcpResetPlanError("disposable namespace acknowledgement is required")
    plan = build_tcp_reset_plan(
        namespace=namespace,
        destination=destination,
        port=port,
        rule_id=rule_id,
        duration_seconds=duration_seconds,
    )
    assert plan.duration_seconds is not None

    interrupted = False
    apply_error: Exception | None = None
    cleanup_error: BaseException | None = None
    apply_attempted = False
    try:
        apply_attempted = True
        _run(plan.apply_argv)
        try:
            time.sleep(plan.duration_seconds)
        except KeyboardInterrupt:
            interrupted = True
    except KeyboardInterrupt:
        interrupted = True
    except Exception as exc:
        apply_error = exc
    finally:
        if apply_attempted:
            try:
                _run(plan.cleanup_argv)
            except (Exception, KeyboardInterrupt) as exc:
                cleanup_error = exc

    if cleanup_error is not None:
        if apply_error is not None:
            raise RuntimeError(
                "TCP reset apply failed and cleanup could not be confirmed"
            ) from cleanup_error
        raise RuntimeError("TCP reset cleanup failed") from cleanup_error
    if apply_error is not None:
        raise RuntimeError("TCP reset apply failed") from apply_error
    return 130 if interrupted else 0


def clear_tcp_reset(
    *,
    namespace: str,
    destination: str,
    port: int,
    rule_id: str,
    confirm_disposable_namespace: bool,
) -> None:
    """Remove the exact reset rule generated for this case."""

    if confirm_disposable_namespace is not True:
        raise TcpResetPlanError("disposable namespace acknowledgement is required")
    plan = build_tcp_reset_plan(
        namespace=namespace,
        destination=destination,
        port=port,
        rule_id=rule_id,
        duration_seconds=None,
    )
    _run(plan.cleanup_argv)


def _add_target_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--namespace", required=True, help="named disposable ip-netns")
    parser.add_argument(
        "--destination",
        required=True,
        help="literal destination IPv4/IPv6 address; hostnames are rejected",
    )
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument(
        "--rule-id",
        required=True,
        help="unique QA case identifier used in the firewall rule comment",
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

    plan = subparsers.add_parser("plan", help="print a read-only TCP-reset plan")
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
        help="apply a bounded TCP-reset rule inside a disposable namespace",
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
        "clear", help="remove the exact TCP-reset rule for a QA case"
    )
    _add_target_arguments(clear)
    _add_execution_acknowledgement(clear)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "plan":
            plan = build_tcp_reset_plan(
                namespace=args.namespace,
                destination=args.destination,
                port=args.port,
                rule_id=args.rule_id,
                duration_seconds=args.duration_seconds,
            )
            if args.json_output:
                print(json.dumps(plan.as_dict(), sort_keys=True))
            else:
                print(f"apply:   {shlex.join(plan.apply_argv)}")
                print(f"cleanup: {shlex.join(plan.cleanup_argv)}")
                if plan.duration_seconds is not None:
                    print(f"duration_seconds: {plan.duration_seconds}")
            return 0

        if args.command == "clear":
            clear_tcp_reset(
                namespace=args.namespace,
                destination=args.destination,
                port=args.port,
                rule_id=args.rule_id,
                confirm_disposable_namespace=args.confirm_disposable_namespace,
            )
            return 0

        assert args.command == "apply"
        return apply_tcp_reset(
            namespace=args.namespace,
            destination=args.destination,
            port=args.port,
            rule_id=args.rule_id,
            duration_seconds=args.duration_seconds,
            confirm_disposable_namespace=args.confirm_disposable_namespace,
        )
    except (TcpResetPlanError, RuntimeError) as exc:
        print(f"network-tcp-reset-injector: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
