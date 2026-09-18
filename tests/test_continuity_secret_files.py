from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "continuity"))

from secret_files import (  # noqa: E402
    MAX_SECRET_FILE_BYTES,
    read_secret_file_or_env,
    redact_stream_url,
)


class ContinuitySecretFileTest(unittest.TestCase):
    def test_regular_file_wins_over_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input_uri"
            path.write_text("rtsp://from-file/live/input\n", encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "INPUT_URI_FILE": str(path),
                    "INPUT_URI": "rtsp://from-env/live/input",
                    "IRLIGHT_SECRET_WAIT_SECONDS": "0",
                },
                clear=False,
            ):
                self.assertEqual(
                    read_secret_file_or_env("INPUT_URI", "default"),
                    "rtsp://from-file/live/input",
                )

    def test_symlink_to_regular_file_remains_supported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target"
            target.write_text("rtsp://from-target/live/input\n", encoding="utf-8")
            link = Path(directory) / "input_uri"
            link.symlink_to(target)
            with patch.dict(
                os.environ,
                {
                    "INPUT_URI_FILE": str(link),
                    "IRLIGHT_SECRET_WAIT_SECONDS": "0",
                },
                clear=False,
            ):
                self.assertEqual(
                    read_secret_file_or_env("INPUT_URI", "default"),
                    "rtsp://from-target/live/input",
                )

    def test_oversized_file_is_rejected_without_path_disclosure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oversized-secret"
            path.write_bytes(b"x" * (MAX_SECRET_FILE_BYTES + 1))
            with patch.dict(
                os.environ,
                {
                    "INPUT_URI_FILE": str(path),
                    "IRLIGHT_SECRET_WAIT_SECONDS": "0",
                },
                clear=False,
            ):
                with self.assertRaisesRegex(RuntimeError, "exceeds size limit") as failure:
                    read_secret_file_or_env("INPUT_URI", "default")
            self.assertNotIn(str(path), str(failure.exception))

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO test requires POSIX mkfifo")
    def test_fifo_is_rejected_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secret-fifo"
            os.mkfifo(path)
            with patch.dict(
                os.environ,
                {
                    "INPUT_URI_FILE": str(path),
                    "IRLIGHT_SECRET_WAIT_SECONDS": "0",
                },
                clear=False,
            ):
                with self.assertRaisesRegex(RuntimeError, "regular file"):
                    read_secret_file_or_env("INPUT_URI", "default")

    def test_invalid_utf8_is_controlled_and_does_not_disclose_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid-secret"
            path.write_bytes(b"\xff\xfe")
            with patch.dict(
                os.environ,
                {
                    "INPUT_URI_FILE": str(path),
                    "IRLIGHT_SECRET_WAIT_SECONDS": "0",
                },
                clear=False,
            ):
                with self.assertRaisesRegex(RuntimeError, "valid UTF-8") as failure:
                    read_secret_file_or_env("INPUT_URI", "default")
            self.assertNotIn(str(path), str(failure.exception))
            self.assertIsNone(failure.exception.__cause__)

    def test_missing_file_error_does_not_disclose_operator_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing-secret"
            with patch.dict(
                os.environ,
                {
                    "INPUT_URI_FILE": str(path),
                    "IRLIGHT_SECRET_WAIT_SECONDS": "0",
                },
                clear=False,
            ):
                with self.assertRaisesRegex(RuntimeError, "cannot read secret file") as failure:
                    read_secret_file_or_env("INPUT_URI", "default")
            self.assertNotIn(str(path), str(failure.exception))
            self.assertIsNone(failure.exception.__cause__)

    def test_non_finite_wait_configuration_does_not_break_valid_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input_uri"
            path.write_text("rtsp://valid/live/input\n", encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "INPUT_URI_FILE": str(path),
                    "IRLIGHT_SECRET_WAIT_SECONDS": "nan",
                },
                clear=False,
            ):
                self.assertEqual(
                    read_secret_file_or_env("INPUT_URI", "default"),
                    "rtsp://valid/live/input",
                )

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
