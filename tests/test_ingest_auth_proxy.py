from __future__ import annotations

import json
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "node-agent"))

from ingest_auth_proxy import (  # noqa: E402
    AuthProxyConfig,
    IngestAuthProxy,
    PositiveAuthCache,
)


class FakeAuthUpstream:
    def __init__(self) -> None:
        self.mode = "allow"
        self.request_count = 0
        self.cache_seconds = 60.0
        self.last_authorization: str | None = None

    def start(self) -> None:
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                upstream.request_count += 1
                upstream.last_authorization = self.headers.get("Authorization")
                if upstream.mode == "deny":
                    body = b'{"detail":"invalid ingest credential"}'
                    self.send_response(401)
                elif upstream.mode == "locked":
                    body = b'{"detail":"ingest authentication temporarily blocked"}'
                    self.send_response(429)
                    self.send_header("Retry-After", "30")
                elif upstream.mode == "error":
                    body = b'{"detail":"temporary failure"}'
                    self.send_response(503)
                else:
                    body = json.dumps(
                        {
                            "authorized": True,
                            "cache_valid_until": time.time() + upstream.cache_seconds,
                        }
                    ).encode("utf-8")
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3.0)


class IngestAuthProxyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.upstream = FakeAuthUpstream()
        self.upstream.start()
        self.proxy = IngestAuthProxy(
            upstream_url=f"http://127.0.0.1:{self.upstream.port}/internal/ingest/auth",
            upstream_token="node-access-token",
            config=AuthProxyConfig(
                listen_host="127.0.0.1",
                listen_port=0,
                upstream_timeout_seconds=0.5,
                cache_max_age_seconds=30.0,
                cache_max_entries=8,
            ),
        )
        self.proxy.start()
        self.url = f"http://127.0.0.1:{self.proxy.port}/auth"
        self.payload = {
            "user": "11111111-1111-4111-8111-111111111111",
            "password": "super-secret-stream-key",
            "token": "",
            "ip": "203.0.113.10",
            "action": "publish",
            "path": "live/input",
            "protocol": "rtmp",
            "id": "publisher-1",
            "query": "",
            "userAgent": "test",
        }

    def test_upstream_request_uses_node_bearer_token(self) -> None:
        status, _body, _headers = self._request()
        self.assertEqual(status, 200)
        self.assertEqual(
            self.upstream.last_authorization,
            "Bearer node-access-token",
        )

    def tearDown(self) -> None:
        self.proxy.stop()
        self.upstream.stop()

    def _request(self, payload: dict[str, object] | None = None) -> tuple[int, dict[str, object], dict[str, str]]:
        body = json.dumps(payload or self.payload).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=2.0) as response:
                raw = response.read()
                headers = dict(response.headers.items())
                return int(response.status), json.loads(raw), headers
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
                headers = dict(exc.headers.items()) if exc.headers else {}
                return int(exc.code), json.loads(raw), headers
            finally:
                exc.close()

    def test_success_primes_cache_and_5xx_falls_back_across_reconnect_id_and_ip(self) -> None:
        status, body, _headers = self._request()
        self.assertEqual(status, 200)
        self.assertTrue(body["authorized"])
        self.assertEqual(self.proxy.cache.size(), 1)

        self.upstream.mode = "error"
        reconnect = dict(self.payload)
        reconnect["id"] = "publisher-2"
        reconnect["ip"] = "198.51.100.20"
        status, body, _headers = self._request(reconnect)
        self.assertEqual(status, 200)
        self.assertTrue(body["authorized"])
        self.assertTrue(body["cached"])

        wrong = dict(reconnect)
        wrong["password"] = "wrong"
        status, _body, headers = self._request(wrong)
        self.assertEqual(status, 503)
        self.assertEqual(headers.get("Retry-After"), "2")

    def test_explicit_denial_evicts_cached_authorization(self) -> None:
        status, _body, _headers = self._request()
        self.assertEqual(status, 200)
        self.assertEqual(self.proxy.cache.size(), 1)

        self.upstream.mode = "deny"
        status, _body, _headers = self._request()
        self.assertEqual(status, 401)
        self.assertEqual(self.proxy.cache.size(), 0)

        self.upstream.mode = "error"
        status, _body, _headers = self._request()
        self.assertEqual(status, 503)

    def test_explicit_lockout_is_relayed_and_evicts_cache(self) -> None:
        self.assertEqual(self._request()[0], 200)
        self.upstream.mode = "locked"
        status, _body, headers = self._request()
        self.assertEqual(status, 429)
        self.assertEqual(headers.get("Retry-After"), "30")
        self.assertEqual(self.proxy.cache.size(), 0)

    def test_relay_read_is_never_cached_or_fallback_authorized(self) -> None:
        read_payload = dict(self.payload)
        read_payload.update(
            {"action": "read", "path": "output/relay", "id": "relay-client-1"}
        )

        status, body, _headers = self._request(read_payload)
        self.assertEqual(status, 200)
        self.assertTrue(body["authorized"])
        self.assertEqual(self.proxy.cache.size(), 0)

        self.upstream.mode = "error"
        status, body, headers = self._request(read_payload)
        self.assertEqual(status, 503)
        self.assertNotIn("authorized", body)
        self.assertEqual(headers.get("Retry-After"), "2")

    def test_cache_is_memory_only_digest_and_locally_capped(self) -> None:
        cache = PositiveAuthCache(max_age_seconds=5.0, max_entries=2)
        now = 100.0
        self.assertTrue(
            cache.store(self.payload, upstream_valid_until=1000.0, now=now)
        )
        self.assertTrue(cache.allowed(self.payload, now=104.9))
        self.assertFalse(cache.allowed(self.payload, now=105.0))
        self.assertNotIn(str(self.payload["password"]), repr(cache._entries))
        self.assertNotIn(str(self.payload["user"]), repr(cache._entries))

    def test_cache_entry_limit_is_bounded(self) -> None:
        cache = PositiveAuthCache(max_age_seconds=60.0, max_entries=2)
        for index in range(3):
            payload = dict(self.payload)
            payload["password"] = f"secret-{index}"
            cache.store(payload, upstream_valid_until=1000.0, now=100.0 + index)
        self.assertEqual(cache.size(now=103.0), 2)


if __name__ == "__main__":
    unittest.main()
