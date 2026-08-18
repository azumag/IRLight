from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from ingest_api import _connection_info, _public_ingest_host  # noqa: E402


class RTMPSConnectionInfoTest(unittest.TestCase):
    def setUp(self) -> None:
        self.session = {"provider_public_ipv4": "203.0.113.42"}

    def test_stable_dns_host_overrides_ephemeral_provider_ip(self) -> None:
        with patch.dict(os.environ, {"IRLIGHT_INGEST_PUBLIC_HOST": "ingest.example.test"}):
            self.assertEqual(_public_ingest_host(self.session), "ingest.example.test")

    def test_provider_ip_is_fallback_when_dns_host_is_not_configured(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("IRLIGHT_INGEST_PUBLIC_HOST", None)
            self.assertEqual(_public_ingest_host(self.session), "203.0.113.42")

    def test_rtmps_info_is_exposed_only_when_enabled(self) -> None:
        with patch.dict(
            os.environ,
            {
                "IRLIGHT_INGEST_PUBLIC_HOST": "ingest.example.test",
                "IRLIGHT_INGEST_RTMPS_ENABLED": "1",
                "IRLIGHT_INGEST_RTMPS_PORT": "1936",
            },
        ):
            info = _connection_info(
                self.session,
                "session-user",
                "one-time-secret",
                ["rtmp", "srt"],
            )
        self.assertTrue(info["rtmps"]["enabled"])
        self.assertEqual(
            info["rtmps"]["server_url"],
            "rtmps://ingest.example.test:1936/live/input",
        )
        self.assertEqual(info["rtmps"]["username"], "session-user")
        self.assertEqual(info["rtmps"]["password"], "one-time-secret")

        with patch.dict(
            os.environ,
            {"IRLIGHT_INGEST_PUBLIC_HOST": "ingest.example.test"},
            clear=False,
        ):
            os.environ.pop("IRLIGHT_INGEST_RTMPS_ENABLED", None)
            disabled = _connection_info(
                self.session,
                "session-user",
                "one-time-secret",
                ["rtmp", "srt"],
            )
        self.assertFalse(disabled["rtmps"]["enabled"])
        self.assertIsNone(disabled["rtmps"]["server_url"])
        self.assertIsNone(disabled["rtmps"]["password"])

    def test_srt_only_credential_does_not_advertise_rtmp_or_rtmps(self) -> None:
        with patch.dict(
            os.environ,
            {
                "IRLIGHT_INGEST_PUBLIC_HOST": "ingest.example.test",
                "IRLIGHT_INGEST_RTMPS_ENABLED": "1",
            },
        ):
            info = _connection_info(
                self.session,
                "session-user",
                "one-time-secret",
                ["srt"],
            )
        self.assertFalse(info["rtmp"]["enabled"])
        self.assertIsNone(info["rtmp"]["server_url"])
        self.assertFalse(info["rtmps"]["enabled"])
        self.assertIsNone(info["rtmps"]["server_url"])
        self.assertTrue(info["srt"]["enabled"])
        self.assertIsNotNone(info["srt"]["url"])

    def test_connection_info_never_recovers_secret(self) -> None:
        with patch.dict(
            os.environ,
            {
                "IRLIGHT_INGEST_PUBLIC_HOST": "ingest.example.test",
                "IRLIGHT_INGEST_RTMPS_ENABLED": "1",
            },
        ):
            info = _connection_info(
                self.session,
                "session-user",
                None,
                ["rtmp", "srt"],
            )
        self.assertIsNone(info["rtmp"]["password"])
        self.assertIsNone(info["rtmps"]["password"])
        self.assertFalse(info["rtmps"]["password_available"])


if __name__ == "__main__":
    unittest.main()
