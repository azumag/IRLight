"""Trusted-proxy aware client address resolution for authentication admission.

Authentication admission limits must be keyed on the address of the client,
not on the address of an intermediary.  The ASGI ``client`` entry always
reports the immediate peer, which is the correct answer unless the Control
Plane runs behind a reverse proxy.  A client-supplied ``X-Forwarded-For``
header is attacker controlled, so it is ignored unless the immediate peer is
an explicitly configured trusted proxy.  When it is trusted, the header is
walked right to left and the first hop that is not itself a trusted proxy is
the client.

Defaults are deliberately fail safe: with no configured trusted proxy, no
forwarding header is ever consulted and the peer address is returned.  Only
exact addresses configured through ``IRLIGHT_TRUSTED_PROXIES`` are trusted;
CIDR ranges are not supported so an operator must name each proxy hop.
"""

from __future__ import annotations

import ipaddress
import os
from typing import Any, Iterable

TRUSTED_PROXIES_ENV = "IRLIGHT_TRUSTED_PROXIES"
FORWARDED_FOR_HEADER = "x-forwarded-for"
DEFAULT_MAX_FORWARDED_HOPS = 20
MAX_FORWARDED_HEADER_BYTES = 2048
MAX_ADDRESS_CHARS = 64


def _normalized_address(value: Any) -> str | None:
    """Return the canonical form of a single address, or ``None`` if invalid."""

    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > MAX_ADDRESS_CHARS:
        return None
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def _normalized_addresses(values: Iterable[Any]) -> frozenset[str]:
    normalized = set()
    for value in values:
        address = _normalized_address(value)
        if address is not None:
            normalized.add(address)
    return frozenset(normalized)


def trusted_proxies_from_env() -> frozenset[str]:
    """Parse the configured trusted proxy list, ignoring unusable entries."""

    raw = os.getenv(TRUSTED_PROXIES_ENV, "")
    if not isinstance(raw, str):
        return frozenset()
    return _normalized_addresses(raw.split(","))


def _scope_address(scope: Any) -> str | None:
    if not isinstance(scope, dict):
        return None
    client = scope.get("client")
    if not isinstance(client, (tuple, list)) or len(client) != 2:
        return None
    return _normalized_address(client[0])


def _forwarded_for_value(http_request: Any) -> str | None:
    headers = getattr(http_request, "headers", None)
    if headers is None:
        return None
    try:
        values = headers.getlist(FORWARDED_FOR_HEADER)
    except AttributeError:
        value = headers.get(FORWARDED_FOR_HEADER)
        values = [] if value is None else [value]
    # More than one header field is ambiguous for right-to-left resolution, and
    # an oversized header cannot be resolved within a bounded amount of work.
    if len(values) != 1:
        return None
    value = values[0]
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_FORWARDED_HEADER_BYTES:
        return None
    return value


def client_address(
    http_request: Any,
    *,
    trusted_proxies: Iterable[Any] | None = None,
) -> str | None:
    """Resolve the admission key address for one request.

    Returns ``None`` when the transport did not expose a peer address at all,
    which the caller treats as an unknown (shared) address.
    """

    peer = _scope_address(getattr(http_request, "scope", None))
    if peer is None:
        return None

    trusted = (
        trusted_proxies_from_env()
        if trusted_proxies is None
        else _normalized_addresses(trusted_proxies)
    )
    if not trusted or peer not in trusted:
        return peer

    forwarded = _forwarded_for_value(http_request)
    if forwarded is None:
        return peer
    hops = [hop.strip() for hop in forwarded.split(",")]
    if not hops or len(hops) > DEFAULT_MAX_FORWARDED_HOPS:
        return peer
    for hop in reversed(hops):
        candidate = _normalized_address(hop)
        if candidate is None:
            # A malformed chain is not partially trusted: fall back to the peer
            # instead of guessing which hop the attacker intended to expose.
            return peer
        if candidate not in trusted:
            return candidate
    # Every hop is a trusted proxy, so no real client can be identified without
    # trusting one of them; the peer is the only defensible answer.
    return peer
