from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "node-agent"))

from ingest_auth_proxy import AuthProxyConfig, IngestAuthProxy  # noqa: E402


def _proxy(
    observer,
    *,
    max_readers: int = 1,
    relay_path: str = "output/relay",
) -> IngestAuthProxy:
    return IngestAuthProxy(
        upstream_url="http://127.0.0.1:1/unreachable",
        config=AuthProxyConfig(
            relay_max_readers=max_readers,
            relay_path=relay_path,
        ),
        relay_reader_observer=observer,
    )


def _read_payload() -> dict[str, object]:
    return {
        "user": "session-1",
        "password": "relay-secret",
        "ip": "203.0.113.10",
        "action": "read",
        "path": "output/relay",
        "protocol": "rtmp",
        "id": "reader-1",
    }


class RelayReadLimitTest(unittest.TestCase):
    def test_zero_readers_falls_through_to_upstream(self) -> None:
        proxy = _proxy(lambda: _observation("DISCONNECTED", 0))
        self.assertIsNone(proxy._relay_read_limited(_read_payload()))

    def test_reader_at_cap_is_denied(self) -> None:
        proxy = _proxy(lambda: _observation("CONNECTED", 1))
        response = proxy._relay_read_limited(_read_payload())
        self.assertIsNotNone(response)
        self.assertEqual(response.status, 403)
        self.assertIn(b"limit", response.body)

    def test_extra_reader_within_cap_is_admitted(self) -> None:
        proxy = _proxy(
            lambda: _observation("CONNECTED", 1), max_readers=2
        )
        self.assertIsNone(proxy._relay_read_limited(_read_payload()))

    def test_untrusted_observation_fails_closed(self) -> None:
        for broken in (
            None,
            {},
            {"status": "UNKNOWN", "reader_count": 0, "reason_code": "x"},
            {"status": "CONNECTED", "reader_count": "1", "reason_code": None},
            {"status": "CONNECTED", "reader_count": -1, "reason_code": None},
            {"status": "CONNECTED", "reader_count": True, "reason_code": None},
            {"status": "CONNECTED", "reader_count": 0, "reason_code": "ERR"},
        ):
            with self.subTest(broken=broken):
                proxy = _proxy(lambda broken=broken: broken)
                response = proxy._relay_read_limited(_read_payload())
                self.assertIsNotNone(response)
                self.assertEqual(response.status, 503)
                self.assertEqual(response.headers.get("Retry-After"), "2")

    def test_observer_exception_fails_closed(self) -> None:
        def explode() -> dict[str, object]:
            raise RuntimeError("MediaMTX API unavailable")

        proxy = _proxy(explode)
        response = proxy._relay_read_limited(_read_payload())
        self.assertIsNotNone(response)
        self.assertEqual(response.status, 503)

    def test_offline_relay_path_has_no_readers_to_cap(self) -> None:
        proxy = _proxy(
            lambda: {
                "status": "UNKNOWN",
                "reader_count": 0,
                "reason_code": "RELAY_SOURCE_OFFLINE",
            }
        )
        self.assertIsNone(proxy._relay_read_limited(_read_payload()))

    def test_non_relay_requests_are_not_limited(self) -> None:
        proxy = _proxy(lambda: _observation("CONNECTED", 32))
        cases = (
            {**_read_payload(), "action": "publish", "path": "live/input"},
            {**_read_payload(), "path": "live/input"},
            {**_read_payload(), "protocol": "srt"},
            {**_read_payload(), "protocol": "rtsp"},
        )
        for payload in cases:
            with self.subTest(payload=payload):
                self.assertIsNone(proxy._relay_read_limited(payload))

    def test_custom_relay_path_is_matched(self) -> None:
        proxy = _proxy(
            lambda: _observation("CONNECTED", 5),
            relay_path="custom/relay",
        )
        payload = {**_read_payload(), "path": "custom/relay"}
        response = proxy._relay_read_limited(payload)
        self.assertIsNotNone(response)
        self.assertEqual(response.status, 403)

    def test_denied_request_never_reaches_upstream(self) -> None:
        proxy = _proxy(lambda: _observation("CONNECTED", 1))
        with patch(
            "urllib.request.urlopen",
            side_effect=AssertionError("upstream must not be called"),
        ):
            response = proxy.authorize(_read_payload())
        self.assertEqual(response.status, 403)

    def test_env_config_defaults_and_bounds(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("NODE_RELAY_MAX_READERS", None)
            os.environ.pop("NODE_RELAY_PATH", None)
            config = AuthProxyConfig.from_env()
        self.assertEqual(config.relay_max_readers, 1)
        self.assertEqual(config.relay_path, "output/relay")

        for raw, expected in (
            ("0", 1),
            ("4", 4),
            ("64", 32),
            ("abc", 1),
            ("-3", 1),
        ):
            with self.subTest(raw=raw), patch.dict(
                "os.environ", {"NODE_RELAY_MAX_READERS": raw}
            ):
                self.assertEqual(
                    AuthProxyConfig.from_env().relay_max_readers, expected
                )


def _observation(status: str, readers: int) -> dict[str, object]:
    return {
        "status": status,
        "connected": status == "CONNECTED",
        "reader_count": readers,
        "reason_code": None,
        "observed_at": 0.0,
    }


if __name__ == "__main__":
    unittest.main()
