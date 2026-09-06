"""Secret-safety checks for user-controlled destination URLs.

This module deliberately performs no network I/O. It is shared by catalog
persistence and destination verification so a URL that is unsafe to probe is
also unsafe to persist or return from the catalog.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, unquote_plus, urlsplit


SENSITIVE_SRT_QUERY_KEYS = {"passphrase"}
PUBLIC_SRT_QUERY_KEYS = {"conntimeo", "latency", "mode", "streamid"}
ROUTING_VALUE_RE = re.compile(r"[A-Za-z0-9._/-]{1,200}\Z")


class DestinationUrlSafetyError(ValueError):
    """A destination URL contains material that is unsafe to persist or probe."""


def validate_destination_url_secret_safety(url: str) -> None:
    """Accept only explicitly public Destination URL forms.

    Ordinary routing-only SRT stream IDs such as ``publish:probe`` remain
    supported. Authentication-bearing and ambiguous forms are rejected until a
    non-argv secret delivery path exists. SRT query names and values are both
    allowlisted so an unknown option cannot become an unvalidated credential
    channel through parser differences or percent encoding.
    """

    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise DestinationUrlSafetyError("destination URL is invalid") from exc

    if parsed.username is not None or parsed.password is not None:
        raise DestinationUrlSafetyError(
            "credentials must not be embedded in destination URL"
        )

    if parsed.scheme.casefold() != "srt":
        return

    seen_keys: set[str] = set()
    for raw_key, raw_value in parse_qsl(parsed.query, keep_blank_values=True):
        key = _decode_repeated(raw_key).strip().casefold()
        value = _decode_repeated(raw_value).strip()
        if key in seen_keys:
            raise DestinationUrlSafetyError(
                "SRT destination must not contain duplicate query parameters"
            )
        seen_keys.add(key)

        if key in SENSITIVE_SRT_QUERY_KEYS:
            raise DestinationUrlSafetyError(
                "SRT secrets must be configured separately from server_url"
            )
        if key not in PUBLIC_SRT_QUERY_KEYS:
            raise DestinationUrlSafetyError(
                "SRT destination contains an unsupported query parameter"
            )
        _validate_public_query_value(key, value)


def _decode_repeated(value: str) -> str:
    decoded = value
    for _ in range(8):
        next_value = unquote_plus(decoded)
        if next_value == decoded:
            return decoded
        decoded = next_value
    raise DestinationUrlSafetyError("SRT destination query is over-encoded")


def _validate_public_query_value(key: str, value: str) -> None:
    if key == "streamid":
        _validate_streamid(value)
        return
    if key == "mode":
        if value.casefold() != "caller":
            raise DestinationUrlSafetyError(
                "SRT destination verification requires caller mode"
            )
        return
    if key in {"latency", "conntimeo"}:
        if not value or not value.isascii() or not value.isdecimal():
            raise DestinationUrlSafetyError(
                f"SRT destination {key} must be a decimal integer"
            )
        return
    raise DestinationUrlSafetyError("SRT destination contains an unsupported query parameter")


def _validate_streamid(value: str) -> None:
    if value.startswith("#!::"):
        _validate_structured_streamid(value)
        return

    prefix, separator, route = value.partition(":")
    if (
        separator != ":"
        or prefix.casefold() != "publish"
        or not ROUTING_VALUE_RE.fullmatch(route)
    ):
        raise DestinationUrlSafetyError(
            "authenticated SRT streamid must not be embedded in server_url"
        )


def _validate_structured_streamid(value: str) -> None:
    fields: dict[str, str] = {}
    for field in value[4:].split(","):
        key, separator, field_value = field.partition("=")
        key = key.strip().casefold()
        field_value = field_value.strip()
        if separator != "=" or not key or key in fields:
            raise DestinationUrlSafetyError("SRT streamid has invalid public routing syntax")
        if key not in {"m", "r"}:
            raise DestinationUrlSafetyError(
                "authenticated SRT streamid must not be embedded in server_url"
            )
        fields[key] = field_value

    if fields.get("m", "").casefold() != "publish":
        raise DestinationUrlSafetyError("SRT streamid has invalid public routing syntax")
    route = fields.get("r", "")
    if not ROUTING_VALUE_RE.fullmatch(route):
        raise DestinationUrlSafetyError("SRT streamid has invalid public routing syntax")
