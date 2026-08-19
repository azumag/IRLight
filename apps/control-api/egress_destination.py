"""Resolve a catalog Destination plus secret into a one-time egress URL."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit


class EgressDestinationError(RuntimeError):
    pass


def build_egress_url(destination: dict[str, Any], stream_key: str) -> str:
    """Build a publish URL without mutating or persisting the raw secret.

    RTMP/RTMPS destinations can either include a ``{stream_key}`` placeholder
    or omit it, in which case the secret is appended as one encoded path
    segment. This covers the Twitch/YouTube-style server URL + stream key model
    while still allowing custom RTMP path layouts.
    """
    if not stream_key:
        raise EgressDestinationError("destination secret is empty")
    destination_type = str(destination.get("type", "")).lower()
    if destination_type not in {"rtmp", "rtmps"}:
        raise EgressDestinationError("egress protocol is not supported yet")
    server_url = str(destination.get("server_url", "")).strip()
    parsed = urlsplit(server_url)
    scheme = parsed.scheme.lower()
    if scheme not in {"rtmp", "rtmps"} or not parsed.hostname:
        raise EgressDestinationError("destination server URL is invalid")
    if scheme != destination_type:
        raise EgressDestinationError("destination type does not match server URL scheme")
    if parsed.username is not None or parsed.password is not None:
        raise EgressDestinationError("destination URL must not contain credentials")
    if parsed.fragment:
        raise EgressDestinationError("destination URL must not contain a fragment")

    encoded = quote(stream_key, safe="")
    if "{stream_key}" in server_url:
        return server_url.replace("{stream_key}", encoded)

    path = parsed.path.rstrip("/") + "/" + encoded
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))
