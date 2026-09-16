#!/usr/bin/env python3
"""Emit a deterministic, read-only network fault matrix for IRLight QA.

The matrix expands the bounded profiles supported by
``network-fault-injector.py`` into stable RTMP/SRT case identifiers. It never
executes ``tc`` or creates a namespace; each case contains only the exact
namespaced argv that a later isolated runner may execute.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Sequence

PROTOCOL_CHOICES = ("rtmp", "srt")
DEFAULT_PROFILE_DURATION_SECONDS = 30
_SCHEMA_VERSION = 3


class MatrixError(ValueError):
    """Raised when a requested matrix would be ambiguous or unsupported."""


def _load_injector() -> ModuleType:
    path = Path(__file__).resolve().with_name("network-fault-injector.py")
    spec = importlib.util.spec_from_file_location("irlight_network_fault_injector", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("network fault injector module cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


INJECTOR = _load_injector()


def _validated_protocols(protocols: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(dict.fromkeys(protocols))
    if not normalized:
        raise MatrixError("at least one protocol is required")
    unsupported = [protocol for protocol in normalized if protocol not in PROTOCOL_CHOICES]
    if unsupported:
        raise MatrixError(f"unsupported protocol: {unsupported[0]}")
    return normalized


def _validated_bandwidths(bandwidth_kbits: Sequence[int]) -> tuple[int, ...]:
    normalized: list[int] = []
    for bandwidth_kbit in bandwidth_kbits:
        if (
            isinstance(bandwidth_kbit, bool)
            or not isinstance(bandwidth_kbit, int)
            or bandwidth_kbit < INJECTOR.BANDWIDTH_KBIT_MIN
            or bandwidth_kbit > INJECTOR.BANDWIDTH_KBIT_MAX
        ):
            raise MatrixError(
                "bandwidth_kbit must be an integer between "
                f"{INJECTOR.BANDWIDTH_KBIT_MIN} and {INJECTOR.BANDWIDTH_KBIT_MAX}"
            )
        if bandwidth_kbit not in normalized:
            normalized.append(bandwidth_kbit)
    return tuple(normalized)


def _validated_jitter_profiles(
    jitter_profiles: Sequence[tuple[int, int]],
) -> tuple[tuple[int, int], ...]:
    normalized: list[tuple[int, int]] = []
    for profile in jitter_profiles:
        if (
            not isinstance(profile, tuple)
            or len(profile) != 2
            or isinstance(profile[0], bool)
            or isinstance(profile[1], bool)
            or not isinstance(profile[0], int)
            or not isinstance(profile[1], int)
        ):
            raise MatrixError("jitter profile must be a (latency_ms, jitter_ms) integer pair")
        latency_ms, jitter_ms = profile
        if latency_ms not in INJECTOR.LATENCY_MS_CHOICES:
            raise MatrixError("jitter profile latency is outside the supported QA matrix")
        if jitter_ms <= 0 or jitter_ms > latency_ms:
            raise MatrixError("jitter_ms must be positive and no greater than latency_ms")
        pair = (latency_ms, jitter_ms)
        if pair not in normalized:
            normalized.append(pair)
    return tuple(normalized)


def parse_jitter_profile(value: str) -> tuple[int, int]:
    """Parse ``LATENCY_MS:JITTER_MS`` for the repeatable CLI option."""

    parts = value.split(":")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("jitter profile must be LATENCY_MS:JITTER_MS")
    try:
        latency_ms, jitter_ms = (int(part, 10) for part in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "jitter profile must contain integer milliseconds"
        ) from exc
    try:
        return _validated_jitter_profiles(((latency_ms, jitter_ms),))[0]
    except MatrixError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _case(
    *,
    case_id: str,
    protocol: str,
    namespace: str,
    interface: str,
    loss_percent: int | None = None,
    latency_ms: int | None = None,
    jitter_ms: int | None = None,
    bandwidth_kbit: int | None = None,
    disconnect: bool = False,
    duration_seconds: int,
    allow_loopback: bool,
) -> dict[str, object]:
    plan = INJECTOR.build_fault_plan(
        interface=interface,
        namespace=namespace,
        loss_percent=loss_percent,
        latency_ms=latency_ms,
        jitter_ms=jitter_ms,
        disconnect=disconnect,
        duration_seconds=duration_seconds,
        bandwidth_kbit=bandwidth_kbit,
        allow_loopback=allow_loopback,
    )
    return {
        "id": case_id,
        "protocol": protocol,
        "fault": {
            "loss_percent": loss_percent,
            "latency_ms": latency_ms,
            "jitter_ms": jitter_ms,
            "bandwidth_kbit": bandwidth_kbit,
            "disconnect": disconnect,
            "duration_seconds": duration_seconds,
        },
        "plan": plan.as_dict(),
    }


def build_matrix(
    *,
    namespace: str,
    interface: str,
    protocols: Sequence[str] = PROTOCOL_CHOICES,
    profile_duration_seconds: int = DEFAULT_PROFILE_DURATION_SECONDS,
    bandwidth_kbits: Sequence[int] = (),
    jitter_profiles: Sequence[tuple[int, int]] = (),
    allow_loopback: bool = False,
) -> dict[str, object]:
    """Build the baseline #13 matrix plus explicitly selected shaping cases.

    Packet-loss and latency profiles use one caller-selected bounded duration
    (30 seconds by default). Complete disconnects expand every duration listed
    by #13: 10, 30, 120, and 600 seconds. #13 does not define canonical
    bandwidth or jitter values, so those cases are additive only when the caller
    supplies explicit bounded values. Jitter profiles must name both the base
    latency and jitter in milliseconds so no product threshold is invented.
    """

    protocols = _validated_protocols(protocols)
    bandwidth_kbits = _validated_bandwidths(bandwidth_kbits)
    jitter_profiles = _validated_jitter_profiles(jitter_profiles)
    if profile_duration_seconds not in INJECTOR.DURATION_SECONDS_CHOICES:
        raise MatrixError("profile duration is outside the supported QA matrix")

    cases: list[dict[str, object]] = []
    for protocol in protocols:
        for loss_percent in INJECTOR.LOSS_PERCENT_CHOICES:
            cases.append(
                _case(
                    case_id=(
                        f"{protocol}-loss-{loss_percent}pct-"
                        f"{profile_duration_seconds}s"
                    ),
                    protocol=protocol,
                    namespace=namespace,
                    interface=interface,
                    loss_percent=loss_percent,
                    duration_seconds=profile_duration_seconds,
                    allow_loopback=allow_loopback,
                )
            )
        for latency_ms in INJECTOR.LATENCY_MS_CHOICES:
            cases.append(
                _case(
                    case_id=(
                        f"{protocol}-latency-{latency_ms}ms-"
                        f"{profile_duration_seconds}s"
                    ),
                    protocol=protocol,
                    namespace=namespace,
                    interface=interface,
                    latency_ms=latency_ms,
                    duration_seconds=profile_duration_seconds,
                    allow_loopback=allow_loopback,
                )
            )
        for latency_ms, jitter_ms in jitter_profiles:
            cases.append(
                _case(
                    case_id=(
                        f"{protocol}-jitter-{latency_ms}ms-{jitter_ms}ms-"
                        f"{profile_duration_seconds}s"
                    ),
                    protocol=protocol,
                    namespace=namespace,
                    interface=interface,
                    latency_ms=latency_ms,
                    jitter_ms=jitter_ms,
                    duration_seconds=profile_duration_seconds,
                    allow_loopback=allow_loopback,
                )
            )
        for bandwidth_kbit in bandwidth_kbits:
            cases.append(
                _case(
                    case_id=(
                        f"{protocol}-bandwidth-{bandwidth_kbit}kbit-"
                        f"{profile_duration_seconds}s"
                    ),
                    protocol=protocol,
                    namespace=namespace,
                    interface=interface,
                    bandwidth_kbit=bandwidth_kbit,
                    duration_seconds=profile_duration_seconds,
                    allow_loopback=allow_loopback,
                )
            )
        for duration_seconds in INJECTOR.DURATION_SECONDS_CHOICES:
            cases.append(
                _case(
                    case_id=f"{protocol}-disconnect-{duration_seconds}s",
                    protocol=protocol,
                    namespace=namespace,
                    interface=interface,
                    disconnect=True,
                    duration_seconds=duration_seconds,
                    allow_loopback=allow_loopback,
                )
            )

    return {
        "schema_version": _SCHEMA_VERSION,
        "namespace": namespace,
        "interface": interface,
        "protocols": list(protocols),
        "profile_duration_seconds": profile_duration_seconds,
        "bandwidth_kbits": list(bandwidth_kbits),
        "jitter_profiles": [
            {"latency_ms": latency_ms, "jitter_ms": jitter_ms}
            for latency_ms, jitter_ms in jitter_profiles
        ],
        "case_count": len(cases),
        "cases": cases,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", required=True, help="named disposable ip-netns")
    parser.add_argument("--interface", required=True)
    parser.add_argument(
        "--protocol",
        action="append",
        choices=PROTOCOL_CHOICES,
        dest="protocols",
        help="protocol tag to include; repeat to select both (default: rtmp + srt)",
    )
    parser.add_argument(
        "--duration",
        type=int,
        choices=INJECTOR.DURATION_SECONDS_CHOICES,
        default=DEFAULT_PROFILE_DURATION_SECONDS,
        dest="profile_duration_seconds",
        help="duration used for loss, latency, jitter, and explicit bandwidth profiles",
    )
    parser.add_argument(
        "--jitter-profile",
        type=parse_jitter_profile,
        action="append",
        dest="jitter_profiles",
        metavar="LATENCY_MS:JITTER_MS",
        help="add an explicit latency+jitter case; repeatable",
    )
    parser.add_argument(
        "--bandwidth-kbit",
        type=int,
        action="append",
        dest="bandwidth_kbits",
        metavar="KBIT",
        help=(
            "add an explicit bandwidth case; repeatable and bounded to "
            f"{INJECTOR.BANDWIDTH_KBIT_MIN}..{INJECTOR.BANDWIDTH_KBIT_MAX} kbit/s"
        ),
    )
    parser.add_argument("--allow-loopback", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        payload = build_matrix(
            namespace=args.namespace,
            interface=args.interface,
            protocols=args.protocols or PROTOCOL_CHOICES,
            profile_duration_seconds=args.profile_duration_seconds,
            bandwidth_kbits=args.bandwidth_kbits or (),
            jitter_profiles=args.jitter_profiles or (),
            allow_loopback=args.allow_loopback,
        )
    except (MatrixError, INJECTOR.FaultPlanError) as exc:
        print(f"network-fault-matrix: {exc}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2 if args.pretty else None,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
