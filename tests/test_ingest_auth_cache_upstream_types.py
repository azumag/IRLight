from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "node-agent"))

from ingest_auth_proxy import (  # noqa: E402
    AuthProxyConfig,
    IngestAuthProxy,
    PositiveAuthCache,
)


class IngestAuthCacheUpstreamTypeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = {
            "user": "11111111-1111-4111-8111-111111111111",
            "password": "dummy-secret",
            "token": "",
            "action": "publish",
            "path": "live/input",
            "protocol": "rtmp",
        }

    def _authorize_with_body(self, body: bytes) -> tuple[int, PositiveAuthCache]:
        class Response:
            status = 200
            headers: dict[str, str] = {}

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *args: object) -> bool:
                return False

            def read(self) -> bytes:
                return body

        cache = PositiveAuthCache(max_age_seconds=30.0, max_entries=8)
        proxy = IngestAuthProxy(
            upstream_url="http://127.0.0.1:1/internal/ingest/auth",
            config=AuthProxyConfig(
                upstream_timeout_seconds=0.1,
                cache_max_age_seconds=30.0,
                cache_max_entries=8,
            ),
            cache=cache,
        )
        with patch(
            "ingest_auth_proxy.urllib.request.urlopen",
            return_value=Response(),
        ), patch("ingest_auth_proxy.time.time", return_value=100.0):
            response = proxy.authorize(self.payload)
        return response.status, cache

    def test_numeric_upstream_valid_until_primes_cache(self) -> None:
        status, cache = self._authorize_with_body(
            b'{"authorized":true,"cache_valid_until":200}'
        )
        self.assertEqual(status, 200)
        self.assertTrue(cache.allowed(self.payload, now=100.0))

    def test_string_upstream_valid_until_does_not_prime_cache(self) -> None:
        status, cache = self._authorize_with_body(
            b'{"authorized":true,"cache_valid_until":"200"}'
        )
        self.assertEqual(status, 200)
        self.assertEqual(list(cache._entries.items()), [])


if __name__ == "__main__":
    unittest.main()
