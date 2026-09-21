from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "egress-gateway"))

from rtmp_sink import (  # noqa: E402
    DEFAULT_RTMP_SINK_FACTORY,
    RTMP2_SINK_FACTORY,
    destination_url_for_sink,
    parse_rtmp_sink_factory,
    sink_progress,
)


class RtmpSinkSelectionTest(unittest.TestCase):
    def test_legacy_sink_remains_default(self) -> None:
        self.assertEqual(parse_rtmp_sink_factory(None), DEFAULT_RTMP_SINK_FACTORY)
        self.assertEqual(parse_rtmp_sink_factory(""), DEFAULT_RTMP_SINK_FACTORY)

    def test_rtmp2_is_explicitly_allowed(self) -> None:
        self.assertEqual(parse_rtmp_sink_factory("rtmp2sink"), RTMP2_SINK_FACTORY)

    def test_arbitrary_plugin_name_is_rejected(self) -> None:
        for value in ("fakesink", "filesink", "rtmp2sink extra=true"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_rtmp_sink_factory(value)

    def test_legacy_default_librtmp_timeout_leaves_reconnect_margin(self) -> None:
        secret_url = "rtmps://live.example/app/private-stream-key"
        self.assertEqual(
            destination_url_for_sink(
                secret_url,
                sink_factory=DEFAULT_RTMP_SINK_FACTORY,
                librtmp_timeout_raw=None,
            ),
            f"{secret_url} timeout=20",
        )

    def test_legacy_librtmp_timeout_is_not_appended_to_rtmp2_url(self) -> None:
        secret_url = "rtmps://live.example/app/private-stream-key"
        self.assertEqual(
            destination_url_for_sink(
                secret_url,
                sink_factory=RTMP2_SINK_FACTORY,
                librtmp_timeout_raw="30",
            ),
            secret_url,
        )
        self.assertEqual(
            destination_url_for_sink(
                secret_url,
                sink_factory=DEFAULT_RTMP_SINK_FACTORY,
                librtmp_timeout_raw="30",
            ),
            f"{secret_url} timeout=30",
        )


class RtmpSinkComposeTest(unittest.TestCase):
    def test_production_compose_exposes_opt_in_with_legacy_default(self) -> None:
        compose = (ROOT / "docker-compose.node.yml").read_text(encoding="utf-8")
        self.assertIn(
            "EGRESS_RTMP_SINK_FACTORY: ${EGRESS_RTMP_SINK_FACTORY:-rtmpsink}",
            compose,
        )


class RtmpSinkProgressTest(unittest.TestCase):
    def test_legacy_progress_preserves_rendered_semantics(self) -> None:
        progress = sink_progress("rtmpsink", {"rendered": 12})
        self.assertTrue(progress.ready)
        self.assertEqual(progress.rendered_buffers, 12)
        self.assertEqual(progress.progress_marker, (12, 0))

    def test_rtmp2_requires_media_and_transport_progress(self) -> None:
        no_media = sink_progress(
            "rtmp2sink",
            {"out-bytes-total": 1024, "out-bytes-acked": 512},
            observed_sink_buffers=0,
        )
        self.assertFalse(no_media.ready)

        no_transport = sink_progress(
            "rtmp2sink",
            {"out-bytes-total": 0, "out-bytes-acked": 0},
            observed_sink_buffers=3,
        )
        self.assertFalse(no_transport.ready)

        progress = sink_progress(
            "rtmp2sink",
            {"out-bytes-total": 2048, "out-bytes-acked": 1024},
            observed_sink_buffers=3,
        )
        self.assertTrue(progress.ready)
        self.assertEqual(progress.rendered_buffers, 3)
        self.assertEqual(progress.progress_marker, (2048, 1024))
        self.assertEqual(progress.transport_bytes_out, 2048)
        self.assertEqual(progress.transport_bytes_acked, 1024)

    def test_rtmp2_ack_change_is_visible_to_liveness_marker(self) -> None:
        first = sink_progress(
            "rtmp2sink",
            {"out-bytes-total": 4096, "out-bytes-acked": 0},
            observed_sink_buffers=4,
        )
        second = sink_progress(
            "rtmp2sink",
            {"out-bytes-total": 4096, "out-bytes-acked": 2048},
            observed_sink_buffers=4,
        )
        self.assertNotEqual(first.progress_marker, second.progress_marker)

    def test_malformed_rtmp2_transport_counter_does_not_become_progress(self) -> None:
        for malformed in (True, "2048", 2048.5, None):
            with self.subTest(value=malformed):
                progress = sink_progress(
                    "rtmp2sink",
                    {"out-bytes-total": malformed, "out-bytes-acked": 1024},
                    observed_sink_buffers=3,
                )
                self.assertFalse(progress.ready)
                self.assertEqual(progress.transport_bytes_out, 0)
                self.assertEqual(progress.progress_marker, (0, 1024))

    def test_malformed_rtmp2_ack_counter_does_not_become_progress(self) -> None:
        for malformed in (True, "1024", 1024.5, None):
            with self.subTest(value=malformed):
                progress = sink_progress(
                    "rtmp2sink",
                    {"out-bytes-total": 4096, "out-bytes-acked": malformed},
                    observed_sink_buffers=3,
                )
                self.assertTrue(progress.ready)
                self.assertEqual(progress.transport_bytes_acked, 0)
                self.assertEqual(progress.progress_marker, (4096, 0))

    def test_malformed_legacy_rendered_counter_does_not_become_progress(self) -> None:
        for malformed in (True, "12", 12.5, None):
            with self.subTest(value=malformed):
                progress = sink_progress("rtmpsink", {"rendered": malformed})
                self.assertFalse(progress.ready)
                self.assertEqual(progress.rendered_buffers, 0)
                self.assertEqual(progress.progress_marker, (0, 0))

    def test_negative_counters_remain_fail_closed(self) -> None:
        progress = sink_progress(
            "rtmp2sink",
            {"out-bytes-total": -1, "out-bytes-acked": -2},
            observed_sink_buffers=-3,
        )
        self.assertFalse(progress.ready)
        self.assertEqual(progress.rendered_buffers, 0)
        self.assertEqual(progress.progress_marker, (0, 0))
        self.assertEqual(progress.transport_bytes_out, 0)
        self.assertEqual(progress.transport_bytes_acked, 0)


if __name__ == "__main__":
    unittest.main()
