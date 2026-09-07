from __future__ import annotations

import tempfile
import unittest
from importlib.metadata import version
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse


class WebStackSecurityTest(unittest.IsolatedAsyncioTestCase):
    """Exercise the installed framework without a server or external requests."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.fixture = Path(self.directory.name) / "index.html"
        self.content = b"IRLight web stack regression fixture"
        self.fixture.write_bytes(self.content)
        self.app = FastAPI()

        @self.app.get("/path")
        async def request_path(request: Request) -> dict[str, str]:
            return {"path": request.url.path}

        @self.app.api_route("/file", methods=["GET", "HEAD"])
        async def file_response() -> FileResponse:
            return FileResponse(self.fixture)

    async def request(self, path: str, *, method: str = "GET", headers=()):
        messages = []
        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1", "scheme": "http", "method": method,
            "path": path, "raw_path": path.encode(), "root_path": "",
            "query_string": b"", "headers": list(headers),
            "server": ("testserver", 80), "client": ("127.0.0.1", 12345),
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        await self.app(scope, receive, send)
        start = next(message for message in messages if message["type"] == "http.response.start")
        body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
        return start["status"], dict(start["headers"]), body

    def test_installed_starlette_excludes_known_vulnerable_releases(self) -> None:
        # Range parsing: GHSA-7f5h-v6xp-fcq8, fixed in 0.49.1.
        # Host reconstruction: GHSA-86qp-5c8j-p5mr, fixed in 1.0.1.
        release = tuple(int(part) for part in version("starlette").split("."))
        self.assertGreaterEqual(release, (1, 0, 1))

    async def test_malformed_host_cannot_replace_routed_path(self) -> None:
        import json

        for host in (b"example.test/healthz?x=", b"example.test?x=", b"example.test#fragment"):
            with self.subTest(host=host):
                status, _, body = await self.request("/path", headers=[(b"host", host)])
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)["path"], "/path")

    async def test_valid_host_preserves_routed_path(self) -> None:
        import json

        for host in (b"example.test:8080", b"[::1]:8080"):
            with self.subTest(host=host):
                status, _, body = await self.request("/path", headers=[(b"host", host)])
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)["path"], "/path")

    async def test_file_response_serves_complete_body(self) -> None:
        status, headers, body = await self.request("/file")
        self.assertEqual(status, 200)
        self.assertEqual(body, self.content)
        self.assertEqual(int(headers[b"content-length"]), len(self.content))

    async def test_file_response_preserves_single_range(self) -> None:
        status, headers, body = await self.request("/file", headers=[(b"range", b"bytes=0-3")])
        self.assertEqual(status, 206)
        self.assertEqual(body, self.content[:4])
        self.assertEqual(headers[b"content-range"], f"bytes 0-3/{len(self.content)}".encode())

    async def test_excessive_ranges_fall_back_to_bounded_full_response(self) -> None:
        # More than the patched framework's 100-range limit. Repeated valid
        # ranges stay within this tiny fixture and do not require load testing.
        header = b"bytes=" + b",".join([b"0-0", b"3-3"] * 100)
        status, headers, body = await self.request("/file", headers=[(b"range", header)])
        self.assertEqual(status, 200)
        self.assertEqual(body, self.content)
        self.assertEqual(int(headers[b"content-length"]), len(self.content))
        self.assertNotIn(b"content-range", headers)

    async def test_file_response_head_does_not_send_body(self) -> None:
        status, headers, body = await self.request("/file", method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertEqual(int(headers[b"content-length"]), len(self.content))

    async def test_file_response_rejects_unsatisfiable_range(self) -> None:
        status, headers, _ = await self.request("/file", headers=[(b"range", b"bytes=99999-")])
        self.assertEqual(status, 416)
        self.assertEqual(headers[b"content-range"], f"bytes */{len(self.content)}".encode())
