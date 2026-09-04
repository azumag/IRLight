from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "continuity"))

from secret_files import redact_stream_url  # noqa: E402


class ContinuitySecretFileTest(unittest.TestCase):
    def test_redaction_removes_userinfo_path_query_and_fragment(self) -> None:
        protected = (
            "rtsp://irlight-internal:super-secret@mediamtx:8554/"
            "live/input?token=query-secret#fragment-secret"
        )
        redacted = redact_stream_url(protected)

        self.assertEqual(redacted, "rtsp://mediamtx:8554/…")
        for secret in (
            "irlight-internal",
            "super-secret",
            "live/input",
            "query-secret",
            "fragment-secret",
        ):
            self.assertNotIn(secret, redacted)

    def test_redaction_handles_ipv6_and_rejects_malformed_urls(self) -> None:
        self.assertEqual(
            redact_stream_url("rtmps://user:key@[2001:db8::1]:443/app/key"),
            "rtmps://[2001:db8::1]:443/…",
        )
        self.assertEqual(redact_stream_url("not-a-stream-url"), "<configured>")
        self.assertEqual(redact_stream_url("rtmp://host:bad/path"), "<configured>")


if __name__ == "__main__":
    unittest.main()
