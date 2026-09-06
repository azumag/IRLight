"""Safety limits for user-authored Session event payloads.

These checks deliberately cover the persisted JSON shape only. They do not replace
an ASGI/proxy raw request-body limit, which must reject oversized bodies before
FastAPI parses them.
"""

from __future__ import annotations

import json
import math
from typing import Any


USER_EVENT_MAX_PAYLOAD_BYTES = 8 * 1024
USER_EVENT_MAX_DEPTH = 8
USER_EVENT_MAX_ELEMENTS = 128


class UserEventPayloadError(ValueError):
    """Raised when a user-authored event payload exceeds the safe persistence budget."""


def validate_user_event_payload(payload: dict[str, Any]) -> None:
    """Validate a user-authored payload before it reaches the SessionStore.

    The budget is intentionally independent from internal Session events. Internal
    producers are trusted code paths and may have schemas that do not fit this
    user-facing limit.
    """

    if not isinstance(payload, dict):
        raise UserEventPayloadError("payload must be an object")

    elements = 0
    stack: list[tuple[Any, int]] = [(payload, 1)]
    while stack:
        value, depth = stack.pop()
        if isinstance(value, dict):
            if depth > USER_EVENT_MAX_DEPTH:
                raise UserEventPayloadError("payload nesting is too deep")
            elements += len(value)
            if elements > USER_EVENT_MAX_ELEMENTS:
                raise UserEventPayloadError("payload contains too many elements")
            for key, child in value.items():
                if not isinstance(key, str):
                    raise UserEventPayloadError("payload keys must be strings")
                if isinstance(child, (dict, list)):
                    stack.append((child, depth + 1))
                else:
                    _validate_scalar(child)
        elif isinstance(value, list):
            if depth > USER_EVENT_MAX_DEPTH:
                raise UserEventPayloadError("payload nesting is too deep")
            elements += len(value)
            if elements > USER_EVENT_MAX_ELEMENTS:
                raise UserEventPayloadError("payload contains too many elements")
            for child in value:
                if isinstance(child, (dict, list)):
                    stack.append((child, depth + 1))
                else:
                    _validate_scalar(child)
        else:
            _validate_scalar(value)

    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise UserEventPayloadError("payload is not bounded JSON") from exc
    if len(encoded) > USER_EVENT_MAX_PAYLOAD_BYTES:
        raise UserEventPayloadError("payload is too large")


def _validate_scalar(value: Any) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if math.isfinite(value):
            return
        raise UserEventPayloadError("payload contains a non-finite number")
    raise UserEventPayloadError("payload contains a non-JSON value")
