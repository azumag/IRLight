"""Safety limits for user-authored Session event requests and payloads."""

from __future__ import annotations

import json
import math
from typing import Any, Awaitable, Callable


USER_EVENT_MAX_PAYLOAD_BYTES = 8 * 1024
# Keep the pre-parse transport budget comfortably above the persisted payload
# budget. 64 KiB preserves normal JSON encodings (including escaped Unicode and
# the request envelope) while preventing whitespace/duplicate-key inflation from
# making FastAPI buffer an arbitrarily large body for this endpoint.
USER_EVENT_MAX_REQUEST_BYTES = 64 * 1024
USER_EVENT_MAX_DEPTH = 8
USER_EVENT_MAX_ELEMENTS = 128
USER_EVENT_REQUEST_TOO_LARGE_CODE = "USER_EVENT_REQUEST_TOO_LARGE"


class UserEventBodyLimitMiddleware:
    """Bound raw user-event request bodies before FastAPI/Pydantic parses them.

    The middleware is deliberately scoped to ``POST /v1/sessions/{id}/events``.
    It uses both Content-Length (when present) and a bounded pre-parse buffer, so
    a missing or dishonest header cannot bypass the limit. Oversized bodies are
    rejected before the downstream FastAPI application sees any request body.
    """

    def __init__(
        self,
        app: Callable[..., Awaitable[None]],
        max_request_bytes: int = USER_EVENT_MAX_REQUEST_BYTES,
    ) -> None:
        if (
            isinstance(max_request_bytes, bool)
            or not isinstance(max_request_bytes, int)
            or max_request_bytes <= 0
        ):
            raise ValueError("max_request_bytes must be a positive integer")
        self.app = app
        self.max_request_bytes = max_request_bytes

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if not _is_user_event_post(scope):
            await self.app(scope, receive, send)
            return

        if _declared_body_exceeds_limit(scope, self.max_request_bytes):
            await _send_request_too_large(send)
            return

        # Read the bounded raw body ourselves before entering FastAPI. Raising
        # from a wrapped receive() is not sufficient because FastAPI converts
        # body-read exceptions into a generic 400 before outer middleware can
        # map them to the intended fixed 413 contract. A single bytearray also
        # keeps an attacker from consuming memory with arbitrarily many empty
        # ASGI chunks while remaining under the byte limit.
        buffered = bytearray()
        disconnected = False
        while True:
            message = await receive()
            message_type = message.get("type")
            if message_type == "http.disconnect":
                disconnected = True
                break
            if message_type != "http.request":
                continue

            chunk = message.get("body", b"")
            if not isinstance(chunk, bytes):
                # ASGI requires bytes. Treat a broken receive contract as an
                # invalid oversized request rather than coercing untrusted data.
                await _send_request_too_large(send)
                return
            if len(chunk) > self.max_request_bytes - len(buffered):
                await _send_request_too_large(send)
                return
            buffered.extend(chunk)
            if not message.get("more_body", False):
                break

        replayed_body = False
        replayed_disconnect = False

        async def buffered_receive() -> dict[str, Any]:
            nonlocal replayed_body, replayed_disconnect
            if not replayed_body:
                replayed_body = True
                return {
                    "type": "http.request",
                    "body": bytes(buffered),
                    "more_body": disconnected,
                }
            if disconnected and not replayed_disconnect:
                replayed_disconnect = True
                return {"type": "http.disconnect"}
            return await receive()

        await self.app(scope, buffered_receive, send)


def _is_user_event_post(scope: dict[str, Any]) -> bool:
    if scope.get("type") != "http" or str(scope.get("method", "")).upper() != "POST":
        return False
    path = str(scope.get("path", ""))
    if path.endswith("/") and path != "/":
        path = path[:-1]
    parts = path.split("/")
    return (
        len(parts) == 5
        and parts[0] == ""
        and parts[1] == "v1"
        and parts[2] == "sessions"
        and bool(parts[3])
        and parts[4] == "events"
    )


def _declared_body_exceeds_limit(scope: dict[str, Any], limit: int) -> bool:
    for raw_name, raw_value in scope.get("headers", []):
        if raw_name.lower() != b"content-length":
            continue
        try:
            declared = int(raw_value.strip())
        except (TypeError, ValueError):
            # The ASGI server normally rejects malformed Content-Length. The
            # bounded body read remains authoritative if it reaches us.
            continue
        if declared > limit:
            return True
    return False


async def _send_request_too_large(send: Any) -> None:
    body = (
        '{"detail":{"code":"' + USER_EVENT_REQUEST_TOO_LARGE_CODE + '"}}'
    ).encode("ascii")
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


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
