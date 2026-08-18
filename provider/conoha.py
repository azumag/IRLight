"""Common types and IRLight metadata conventions for the ConoHa provider."""

from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal


ENVIRONMENT_VALUES = ("dev", "beta", "prod")
ManagedTag = Literal["true", "false"]


@dataclass(frozen=True)
class SessionMetadata:
    """Resource metadata attached to every IRLight-managed provider resource."""

    session_id: str
    user_id: str
    environment: str
    created_at: float = field(default_factory=time.time)
    delete_after: float | None = None

    def __post_init__(self) -> None:
        if not is_safe_session_id(self.session_id):
            raise ValueError(f"unsafe session id: {self.session_id!r}")
        if not is_safe_user_id(self.user_id):
            raise ValueError(f"unsafe user id: {self.user_id!r}")
        if self.environment not in ENVIRONMENT_VALUES:
            raise ValueError(f"unsupported environment: {self.environment!r}")

    def as_tags(self) -> dict[str, str]:
        tags = {
            "irlight-managed": "true",
            "irlight-session-id": self.session_id,
            "irlight-user-id": self.user_id,
            "irlight-environment": self.environment,
            "irlight-created-at": format_timestamp(self.created_at),
        }
        if self.delete_after is not None:
            tags["irlight-delete-after"] = format_timestamp(self.delete_after)
        return tags


def format_timestamp(epoch: float) -> str:
    """ISO-8601 UTC without sub-second noise, matching provider tag style."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def is_safe_session_id(value: str) -> bool:
    """Session IDs are UUID4 in IRLight; reject anything else to avoid tag abuse."""
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return str(parsed) == value.lower() and parsed.version == 4


def is_safe_user_id(value: str) -> bool:
    """Accept current UUID4 user IDs and legacy lowercase-hex opaque IDs."""
    if not isinstance(value, str) or not value or len(value) > 128:
        return False

    # The auth store issues canonical UUID4 strings. Keep accepting the older
    # short lowercase-hex IDs used by provider/admin spike tests and existing
    # metadata so this validation change is backward compatible.
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        parsed = None
    if parsed is not None and str(parsed) == value.lower() and parsed.version == 4:
        return True

    return value.isalnum() and all(ch in "0123456789abcdef" for ch in value)


def request_id_for(scope: str, session_id: str) -> str:
    """Deterministic idempotency key scoped to one session."""
    if scope not in ("volume", "server"):
        raise ValueError(f"unsupported scope: {scope!r}")
    digest = hashlib.sha256(session_id.encode("ascii")).hexdigest()
    return f"irlight-{scope}-{digest[:24]}"


@dataclass(frozen=True)
class ProviderVolume:
    volume_id: str
    name: str
    size_gb: int
    status: str
    tags: dict[str, str] = field(default_factory=dict)

    @property
    def is_managed(self) -> bool:
        return self.tags.get("irlight-managed") == "true"


@dataclass(frozen=True)
class ProviderServer:
    server_id: str
    name: str
    status: str
    public_ipv4: str | None = None
    tags: dict[str, str] = field(default_factory=dict)

    @property
    def is_managed(self) -> bool:
        return self.tags.get("irlight-managed") == "true"


@dataclass(frozen=True)
class ManagedResource:
    kind: str
    provider_id: str
    session_id: str | None
    user_id: str | None
    created_at: float | None
    delete_after: float | None
    details: dict[str, object] = field(default_factory=dict)


def managed_since(tags: Mapping[str, str]) -> float | None:
    """Parse irlight-created-at tag back into epoch seconds, if present."""
    raw = tags.get("irlight-created-at")
    if not raw:
        return None
    try:
        return time.mktime(time.strptime(raw, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return None
