"""Secret-safety checks for user-controlled destination URLs.

This module deliberately performs no network I/O. It is shared by catalog
persistence and destination verification so a URL that is unsafe to probe is
also unsafe to persist or return from the catalog.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, unquote_plus, urlsplit


SENSITIVE_SRT_QUERY_KEYS = {"passphrase"}
SENSITIVE_STREAMID_NAMED_FIELDS = {
    "p",
    "pass",
    "passwd",
    "password",
    "passphrase",
    "pw",
    "secret",
    "token",
    "u",
    "user",
    "username",
}
SENSITIVE_STREAMID_MARKERS = (
    "password=",
    "passwd=",
    "passphrase=",
    "secret=",
    "token=",
)


class DestinationUrlSafetyError(ValueError):
    """A destination URL contains credential material that must not be stored."""


def validate_destination_url_secret_safety(url: str) -> None:
    """Reject credential-bearing URL forms without exposing their values.

    Ordinary routing-only SRT stream IDs such as ``publish:probe`` remain
    supported. IRLight's authenticated four-part publish stream ID and named
    credential fields are rejected until a non-argv secret delivery path is
    implemented.
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

    seen_streamid = False
    for raw_key, raw_value in parse_qsl(parsed.query, keep_blank_values=True):
        key = _decode_repeated(raw_key).strip().casefold()
        if key in SENSITIVE_SRT_QUERY_KEYS:
            raise DestinationUrlSafetyError(
                "SRT secrets must be configured separately from server_url"
            )
        if key != "streamid":
            continue
        if seen_streamid:
            raise DestinationUrlSafetyError(
                "SRT destination must not contain duplicate streamid parameters"
            )
        seen_streamid = True
        _validate_streamid(raw_value)


def _decode_repeated(value: str) -> str:
    decoded = value
    for _ in range(8):
        next_value = unquote_plus(decoded)
        if next_value == decoded:
            return decoded
        decoded = next_value
    raise DestinationUrlSafetyError("SRT destination query is over-encoded")


def _validate_streamid(value: str) -> None:
    decoded = _decode_repeated(value).strip()
    lowered = decoded.casefold()

    # IRLight/MediaMTX authenticated ingest stream IDs are currently emitted as
    # publish:<path>:<username>:<credential>. Passing that form to
    # srt-live-transmit would expose the credential in the child argv.
    if lowered.startswith("publish:") and decoded.count(":") >= 3:
        raise DestinationUrlSafetyError(
            "authenticated SRT streamid must not be embedded in server_url"
        )

    if any(marker in lowered for marker in SENSITIVE_STREAMID_MARKERS):
        raise DestinationUrlSafetyError(
            "authenticated SRT streamid must not be embedded in server_url"
        )

    # SRT's structured streamid form (#!::k=v,...) can carry user/auth fields.
    # Treat those fields as credential-bearing rather than guessing whether a
    # particular value is harmless.
    if lowered.startswith("#!::"):
        for field in decoded[4:].split(","):
            key = field.split("=", 1)[0].strip().casefold()
            if key in SENSITIVE_STREAMID_NAMED_FIELDS:
                raise DestinationUrlSafetyError(
                    "authenticated SRT streamid must not be embedded in server_url"
                )
