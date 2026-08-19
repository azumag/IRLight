from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit


Resolver = Callable[..., list[tuple[int, int, int, str, tuple[Any, ...]]]]


class DestinationGuardError(RuntimeError):
    def __init__(self, reason_code: str, message: str, *, terminal: bool) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.terminal = terminal


@dataclass(frozen=True)
class DestinationResolution:
    host: str
    port: int
    addresses: tuple[str, ...]


def read_verified_peer_ip(path: str | Path | None) -> str | None:
    if path is None:
        return None
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DestinationGuardError(
            "DESTINATION_GUARD_INVALID",
            "verified destination address metadata is unavailable",
            terminal=True,
        ) from exc
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value.split("%", 1)[0]))
    except ValueError as exc:
        raise DestinationGuardError(
            "DESTINATION_GUARD_INVALID",
            "verified destination address metadata is invalid",
            terminal=True,
        ) from exc


def validate_destination_runtime(
    url: str,
    *,
    expected_peer_ip: str | None = None,
    allow_private_targets: bool = False,
    resolver: Resolver = socket.getaddrinfo,
) -> DestinationResolution:
    """Revalidate the real egress hostname immediately before each attempt.

    Verification already rejects unsafe addresses, but DNS can change between
    verification and stream start. The runtime guard therefore resolves again
    for every publish attempt, rejects the entire answer set if any address is
    unsafe, and optionally requires the peer IP observed during verification to
    still be present.

    The GStreamer/librtmp sink may perform its own DNS lookup after this check;
    this guard intentionally narrows that TOCTOU window and detects persistent
    DNS drift. A connector that can pin the transport IP separately from the TLS
    hostname would be required to eliminate the final lookup race completely.
    """
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    if scheme not in {"rtmp", "rtmps"} or not parsed.hostname:
        raise DestinationGuardError(
            "DESTINATION_GUARD_INVALID",
            "egress destination URL is invalid",
            terminal=True,
        )
    try:
        port = parsed.port or (443 if scheme == "rtmps" else 1935)
    except ValueError as exc:
        raise DestinationGuardError(
            "DESTINATION_GUARD_INVALID",
            "egress destination port is invalid",
            terminal=True,
        ) from exc

    try:
        resolved = resolver(parsed.hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise DestinationGuardError(
            "DNS_FAILED",
            "egress destination hostname could not be resolved",
            terminal=False,
        ) from exc
    except OSError as exc:
        raise DestinationGuardError(
            "DNS_FAILED",
            "egress destination hostname could not be resolved",
            terminal=False,
        ) from exc
    if not resolved:
        raise DestinationGuardError(
            "DNS_FAILED",
            "egress destination hostname could not be resolved",
            terminal=False,
        )

    addresses: list[str] = []
    for entry in resolved:
        sockaddr = entry[4]
        ip_text = str(sockaddr[0]).split("%", 1)[0]
        try:
            address = ipaddress.ip_address(ip_text)
        except ValueError as exc:
            raise DestinationGuardError(
                "DESTINATION_UNSAFE",
                "egress destination resolved to an invalid address",
                terminal=True,
            ) from exc
        if address.is_unspecified or address.is_multicast:
            raise DestinationGuardError(
                "DESTINATION_UNSAFE",
                "egress destination resolved to a disallowed address",
                terminal=True,
            )
        if not allow_private_targets and not address.is_global:
            raise DestinationGuardError(
                "DESTINATION_UNSAFE",
                "egress destination must resolve only to public addresses",
                terminal=True,
            )
        normalized = str(address)
        if normalized not in addresses:
            addresses.append(normalized)

    if expected_peer_ip:
        try:
            expected = str(ipaddress.ip_address(expected_peer_ip.split("%", 1)[0]))
        except ValueError as exc:
            raise DestinationGuardError(
                "DESTINATION_GUARD_INVALID",
                "verified destination address metadata is invalid",
                terminal=True,
            ) from exc
        if expected not in addresses:
            raise DestinationGuardError(
                "DESTINATION_DNS_CHANGED",
                "egress destination DNS answers changed after verification",
                terminal=True,
            )

    return DestinationResolution(parsed.hostname, port, tuple(addresses))
