from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from typing import Any

from fastapi import FastAPI


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from session_event_policy import (  # noqa: E402
    USER_EVENT_MAX_REQUEST_BYTES,
    USER_EVENT_REQUEST_TOO_LARGE_CODE,
    UserEventBodyLimitMiddleware,
)


class _BodyConsumer:
    def __init__(self) -> None:
        self.called = False
        self.completed = False
        self.body = b""

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        del scope
        self.called = True
        chunks: list[bytes] = []
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                break
            if message["type"] != "http.request":
                continue
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        self.body = b"".join(chunks)
        self.completed = True
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})


class UserEventRequestBodyLimitTest(unittest.TestCase):
    def test_oversized_content_length_is_rejected_before_downstream(self) -> None:
        downstream = _BodyConsumer()
        sent = _run_request(
            downstream,
            path="/v1/sessions/session-1/events",
            headers=[
                (b"content-length", str(USER_EVENT_MAX_REQUEST_BYTES + 1).encode())
            ],
            chunks=[b"{}"],
        )

        self.assertFalse(downstream.called)
        self.assertEqual(sent[0]["status"], 413)
        self.assertEqual(
            json.loads(sent[1]["body"]),
            {"detail": {"code": USER_EVENT_REQUEST_TOO_LARGE_CODE}},
        )

    def test_chunked_body_cannot_bypass_limit(self) -> None:
        downstream = _BodyConsumer()
        sent = _run_request(
            downstream,
            path="/v1/sessions/session-1/events",
            chunks=[b"a" * USER_EVENT_MAX_REQUEST_BYTES, b"b"],
        )

        self.assertFalse(downstream.called)
        self.assertFalse(downstream.completed)
        self.assertEqual(sent[0]["status"], 413)

    def test_dishonest_small_content_length_still_hits_streaming_limit(self) -> None:
        downstream = _BodyConsumer()
        sent = _run_request(
            downstream,
            path="/v1/sessions/session-1/events",
            headers=[(b"content-length", b"2")],
            chunks=[b"a" * USER_EVENT_MAX_REQUEST_BYTES, b"b"],
        )

        self.assertFalse(downstream.called)
        self.assertFalse(downstream.completed)
        self.assertEqual(sent[0]["status"], 413)

    def test_exact_limit_is_forwarded_unchanged(self) -> None:
        downstream = _BodyConsumer()
        body = b"x" * USER_EVENT_MAX_REQUEST_BYTES
        sent = _run_request(
            downstream,
            path="/v1/sessions/session-1/events/",
            headers=[(b"content-length", str(len(body)).encode())],
            chunks=[body[:100], body[100:]],
        )

        self.assertTrue(downstream.completed)
        self.assertEqual(downstream.body, body)
        self.assertEqual(sent[0]["status"], 204)

    def test_other_routes_are_not_given_the_user_event_limit(self) -> None:
        downstream = _BodyConsumer()
        body = b"x" * (USER_EVENT_MAX_REQUEST_BYTES + 1)
        sent = _run_request(
            downstream,
            path="/v1/sessions/session-1/prepare",
            headers=[(b"content-length", str(len(body)).encode())],
            chunks=[body],
        )

        self.assertTrue(downstream.completed)
        self.assertEqual(downstream.body, body)
        self.assertEqual(sent[0]["status"], 204)

    def test_get_events_is_not_given_the_post_body_limit(self) -> None:
        downstream = _BodyConsumer()
        body = b"x" * (USER_EVENT_MAX_REQUEST_BYTES + 1)
        sent = _run_request(
            downstream,
            path="/v1/sessions/session-1/events",
            method="GET",
            chunks=[body],
        )

        self.assertTrue(downstream.completed)
        self.assertEqual(sent[0]["status"], 204)

    def test_fastapi_parser_never_sees_oversized_chunked_body(self) -> None:
        app = FastAPI()
        app.add_middleware(UserEventBodyLimitMiddleware, max_request_bytes=16)
        endpoint_calls: list[str] = []

        @app.post("/v1/sessions/{session_id}/events")
        async def event_endpoint(session_id: str, payload: dict[str, Any]) -> dict[str, bool]:
            del payload
            endpoint_calls.append(session_id)
            return {"ok": True}

        sent = _run_request(
            app,
            path="/v1/sessions/session-1/events",
            chunks=[b'{"payload":"1234', b'5678901234567890"}'],
            wrap=False,
        )

        self.assertEqual(endpoint_calls, [])
        self.assertEqual(sent[0]["status"], 413)
        self.assertEqual(
            json.loads(sent[1]["body"]),
            {"detail": {"code": USER_EVENT_REQUEST_TOO_LARGE_CODE}},
        )

    def test_control_api_registers_body_limit_middleware(self) -> None:
        source = (ROOT / "apps" / "control-api" / "app.py").read_text(encoding="utf-8")
        self.assertIn("app.add_middleware(UserEventBodyLimitMiddleware)", source)

    def test_invalid_limit_configuration_fails_closed_at_startup(self) -> None:
        for invalid in (0, -1, True, 1.5):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    UserEventBodyLimitMiddleware(_BodyConsumer(), invalid)  # type: ignore[arg-type]


def _run_request(
    downstream: Any,
    *,
    path: str,
    chunks: list[bytes],
    method: str = "POST",
    headers: list[tuple[bytes, bytes]] | None = None,
    wrap: bool = True,
) -> list[dict[str, Any]]:
    messages = [
        {
            "type": "http.request",
            "body": chunk,
            "more_body": index < len(chunks) - 1,
        }
        for index, chunk in enumerate(chunks)
    ]
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        if messages:
            return messages.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": headers or [],
        "client": ("127.0.0.1", 12345),
        "server": ("test", 80),
        "root_path": "",
    }
    target = UserEventBodyLimitMiddleware(downstream) if wrap else downstream
    asyncio.run(target(scope, receive, send))
    return sent


if __name__ == "__main__":
    unittest.main()
