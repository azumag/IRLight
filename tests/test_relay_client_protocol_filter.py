from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "node-agent"))

from relay_client import RelayClientConfig, RelayClientObserver  # noqa: E402


class RelayClientProtocolFilterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.observer = RelayClientObserver(
            RelayClientConfig(
                api_url="http://mediamtx.invalid:9997",
                path="output/relay",
                timeout_seconds=0.2,
            )
        )

    def _observe(self, readers: list[object]) -> dict[str, object]:
        with patch.object(
            self.observer,
            "_path_snapshot",
            return_value={"online": True, "readers": readers},
        ):
            return self.observer.observe(now=123.0)

    def test_internal_rtsp_and_hls_readers_do_not_consume_relay_slot(self) -> None:
        result = self._observe(
            [
                {"type": "rtspSession", "id": "internal-egress"},
                {"type": "hlsSession", "id": "preview"},
            ]
        )
        self.assertEqual(result["status"], "DISCONNECTED")
        self.assertFalse(result["connected"])
        self.assertEqual(result["reader_count"], 0)

    def test_rtmp_and_rtmps_readers_are_counted(self) -> None:
        result = self._observe(
            [
                {"type": "rtmpConn", "id": "obs-1"},
                {"type": "rtmpsConn", "id": "obs-2"},
                {"type": "rtspSession", "id": "internal-egress"},
            ]
        )
        self.assertEqual(result["status"], "CONNECTED")
        self.assertTrue(result["connected"])
        self.assertEqual(result["reader_count"], 2)

    def test_reader_without_type_is_counted_conservatively(self) -> None:
        result = self._observe([{"id": "legacy-reader"}])
        self.assertEqual(result["status"], "CONNECTED")
        self.assertTrue(result["connected"])
        self.assertEqual(result["reader_count"], 1)


if __name__ == "__main__":
    unittest.main()
