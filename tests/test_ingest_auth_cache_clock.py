from __future__ import annotations

import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "node-agent"))

from ingest_auth_proxy import (  # noqa: E402
    AuthProxyConfig,
    IngestAuthProxy,
    PositiveAuthCache,
)


class IngestAuthCacheClockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = {
            "user": "11111111-1111-4111-8111-111111111111",
            "password": "dummy-secret",
            "token": "",
            "action": "publish",
            "path": "live/input",
            "protocol": "rtmp",
        }

    def _primed_cache(self) -> PositiveAuthCache:
        cache = PositiveAuthCache(max_age_seconds=30.0, max_entries=8)
        self.assertTrue(
            cache.store(self.payload, upstream_valid_until=200.0, now=100.0)
        )
        return cache

    def test_invalid_explicit_clocks_fail_before_cache_mutation(self) -> None:
        cases = [
            ("bool", True),
            ("negative", -1.0),
            ("nan", float("nan")),
            ("positive-infinity", float("inf")),
            ("negative-infinity", float("-inf")),
            ("overflowing-integer", 10**10000),
            ("string", "100"),
        ]
        for label, value in cases:
            with self.subTest(case=label):
                cache = self._primed_cache()
                before = list(cache._entries.items())
                with self.assertRaisesRegex(
                    RuntimeError,
                    "ingest auth cache clock is invalid",
                ):
                    cache.allowed(self.payload, now=value)  # type: ignore[arg-type]
                self.assertEqual(list(cache._entries.items()), before)
                with self.assertRaisesRegex(
                    RuntimeError,
                    "ingest auth cache clock is invalid",
                ):
                    cache.size(now=value)  # type: ignore[arg-type]
                self.assertEqual(list(cache._entries.items()), before)
                second = dict(self.payload)
                second["password"] = f"other-{label}"
                with self.assertRaisesRegex(
                    RuntimeError,
                    "ingest auth cache clock is invalid",
                ):
                    cache.store(
                        second,
                        upstream_valid_until=200.0,
                        now=value,  # type: ignore[arg-type]
                    )
                self.assertEqual(list(cache._entries.items()), before)

    def test_invalid_default_clock_fails_before_cache_mutation(self) -> None:
        cache = self._primed_cache()
        before = list(cache._entries.items())
        with patch("ingest_auth_proxy.time.time", return_value=float("nan")):
            with self.assertRaisesRegex(
                RuntimeError,
                "ingest auth cache clock is invalid",
            ):
                cache.allowed(self.payload)
        self.assertEqual(list(cache._entries.items()), before)

    def test_invalid_upstream_valid_until_never_primes_cache(self) -> None:
        cases = [
            ("bool", True),
            ("negative", -1.0),
            ("nan", float("nan")),
            ("positive-infinity", float("inf")),
            ("negative-infinity", float("-inf")),
            ("overflowing-integer", 10**10000),
            ("string", "200"),
        ]
        for label, value in cases:
            with self.subTest(case=label):
                cache = PositiveAuthCache(max_age_seconds=30.0, max_entries=8)
                with self.assertRaisesRegex(
                    RuntimeError,
                    "ingest auth cache valid-until is invalid",
                ):
                    cache.store(
                        self.payload,
                        upstream_valid_until=value,  # type: ignore[arg-type]
                        now=100.0,
                    )
                self.assertEqual(list(cache._entries.items()), [])

    def test_authoritative_success_does_not_cache_invalid_valid_until(self) -> None:
        class Response:
            status = 200
            headers: dict[str, str] = {}

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *args: object) -> bool:
                return False

            def read(self) -> bytes:
                return b'{"authorized":true,"cache_valid_until":"Infinity"}'

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
        self.assertEqual(response.status, 200)
        self.assertEqual(list(cache._entries.items()), [])

    def test_proxy_fallback_denies_when_cache_clock_is_invalid(self) -> None:
        cache = self._primed_cache()
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
            side_effect=urllib.error.URLError("offline"),
        ), patch("ingest_auth_proxy.time.time", return_value=float("nan")):
            response = proxy.authorize(self.payload)
        self.assertEqual(response.status, 503)
        self.assertEqual(response.headers.get("Retry-After"), "2")
        self.assertNotIn("authorized", json.loads(response.body.decode("utf-8")))


if __name__ == "__main__":
    unittest.main()
