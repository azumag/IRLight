from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "node-agent"))

from ingest_quality import IngestQualityConfig, IngestQualitySampler  # noqa: E402
from runtime_secret_file import MAX_RUNTIME_SECRET_BYTES  # noqa: E402


class IngestQualitySecretFileTest(unittest.TestCase):
    def _sampler(self, path: Path, *, fallback: str = "rtsp://fallback.invalid/live") -> IngestQualitySampler:
        return IngestQualitySampler(
            IngestQualityConfig(
                input_url=fallback,
                input_url_file=path,
            )
        )

    def test_regular_file_is_preferred_over_configured_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample-url"
            path.write_text("rtsp://user:secret@mediamtx:8554/live/input\n", encoding="utf-8")

            self.assertEqual(
                self._sampler(path)._input_url(),
                "rtsp://user:secret@mediamtx:8554/live/input",
            )

    def test_symlink_to_regular_file_remains_supported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "sample-url-v2"
            link = root / "sample-url"
            target.write_text("rtsp://mediamtx:8554/live/projected\n", encoding="utf-8")
            link.symlink_to(target.name)

            self.assertEqual(
                self._sampler(link)._input_url(),
                "rtsp://mediamtx:8554/live/projected",
            )

    def test_fifo_is_rejected_without_attempting_media_probe(self) -> None:
        if not hasattr(os, "mkfifo"):
            self.skipTest("FIFO is unavailable on this platform")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample-url-fifo"
            os.mkfifo(path)

            result = self._sampler(path).sample()

        self.assertEqual(result["reasons"], ["MEDIA_SAMPLE_FAILED"])
        self.assertEqual(result["error"], "ingest sample URL secret is unavailable")

    def test_oversized_file_is_rejected_without_value_or_path_disclosure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "operator-private-sample-url"
            path.write_bytes(b"S" * (MAX_RUNTIME_SECRET_BYTES + 1))

            result = self._sampler(path, fallback="rtsp://must-not-fallback.invalid/live").sample()

        self.assertEqual(result["reasons"], ["MEDIA_SAMPLE_FAILED"])
        self.assertEqual(result["error"], "ingest sample URL secret is unavailable")
        self.assertNotIn(str(path), result["error"])
        self.assertNotIn("must-not-fallback", result["error"])
        self.assertNotIn("SSSS", result["error"])

    def test_invalid_utf8_is_controlled_and_pathless(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "operator-private-sample-url"
            path.write_bytes(b"\xff\xfe")

            result = self._sampler(path).sample()

        self.assertEqual(result["reasons"], ["MEDIA_SAMPLE_FAILED"])
        self.assertEqual(result["error"], "ingest sample URL secret is unavailable")
        self.assertNotIn(str(path), result["error"])

    def test_configured_missing_file_does_not_fallback_to_input_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing-private-sample-url"

            result = self._sampler(
                path,
                fallback="rtsp://fallback-user:fallback-secret@example.invalid/live",
            ).sample()

        self.assertEqual(result["reasons"], ["MEDIA_SAMPLE_FAILED"])
        self.assertEqual(result["error"], "ingest sample URL secret is unavailable")
        self.assertNotIn(str(path), result["error"])
        self.assertNotIn("fallback-secret", result["error"])

    def test_empty_file_preserves_existing_fail_closed_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample-url"
            path.write_text("\n", encoding="utf-8")

            result = self._sampler(path).sample()

        self.assertEqual(result["reasons"], ["MEDIA_SAMPLE_FAILED"])
        self.assertEqual(result["error"], "ingest sample URL secret is empty")


if __name__ == "__main__":
    unittest.main()
