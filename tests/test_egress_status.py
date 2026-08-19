from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "node-agent"))

from egress_status import read_egress_status  # noqa: E402


class EgressStatusReaderTest(unittest.TestCase):
    def test_reader_returns_only_safe_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "egress.json")
            path.write_text(
                json.dumps(
                    {
                        "status": "CONNECTED",
                        "connected": True,
                        "attempt": 2,
                        "reason_code": None,
                        "rendered_buffers": 12,
                        "next_retry_at": None,
                        "destination_scheme": "rtmps",
                        "destination_host": "live.example",
                        "observed_at": 123.0,
                        "credentialed_url": "rtmps://live.example/app/secret-key",
                        "stream_key": "secret-key",
                    }
                ),
                encoding="utf-8",
            )
            result = read_egress_status(path)
        self.assertTrue(result["connected"])
        self.assertEqual(result["status"], "CONNECTED")
        self.assertEqual(result["destination_host"], "live.example")
        self.assertNotIn("credentialed_url", result)
        self.assertNotIn("stream_key", result)
        self.assertNotIn("secret-key", str(result))

    def test_missing_or_corrupt_status_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = read_egress_status(Path(tmp, "missing.json"))
            self.assertEqual(missing["status"], "UNKNOWN")
            self.assertEqual(missing["reason_code"], "STATUS_UNAVAILABLE")
            path = Path(tmp, "egress.json")
            path.write_text("{broken", encoding="utf-8")
            corrupt = read_egress_status(path)
            self.assertEqual(corrupt["status"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
