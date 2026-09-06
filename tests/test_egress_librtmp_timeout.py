from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "egress-gateway"))

from rtmp_session import (  # noqa: E402
    DEFAULT_LIBRTMP_SESSION_TIMEOUT_SECONDS,
    MAX_LIBRTMP_SESSION_TIMEOUT_SECONDS,
    parse_librtmp_session_timeout,
    with_librtmp_session_timeout,
)


class LibrtmpSessionTimeoutTest(unittest.TestCase):
    def test_default_timeout_bounds_remote_outage_detection(self) -> None:
        self.assertEqual(DEFAULT_LIBRTMP_SESSION_TIMEOUT_SECONDS, 30)
        self.assertEqual(parse_librtmp_session_timeout(None), 30)
        self.assertEqual(parse_librtmp_session_timeout(""), 30)

    def test_timeout_rounds_up_and_is_bounded(self) -> None:
        self.assertEqual(parse_librtmp_session_timeout("10.1"), 11)
        self.assertEqual(
            parse_librtmp_session_timeout("999"),
            MAX_LIBRTMP_SESSION_TIMEOUT_SECONDS,
        )
        self.assertEqual(parse_librtmp_session_timeout("0"), 0)
        self.assertEqual(parse_librtmp_session_timeout("-1"), 0)

    def test_invalid_numeric_timeout_is_rejected(self) -> None:
        for raw in ("not-a-number", "nan", "inf", "-inf"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    parse_librtmp_session_timeout(raw)

    def test_timeout_parameter_is_added_without_rewriting_secret_url(self) -> None:
        url = "rtmps://live.example/app/secret-key"
        self.assertEqual(
            with_librtmp_session_timeout(url, 30),
            f"{url} timeout=30",
        )
        self.assertEqual(with_librtmp_session_timeout(url, 0), url)

    def test_whitespace_session_parameter_injection_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            with_librtmp_session_timeout(
                "rtmp://live.example/app/key live=1",
                30,
            )


class EgressDockerEntrypointTest(unittest.TestCase):
    def test_container_uses_timeout_entrypoint(self) -> None:
        dockerfile = (ROOT / "apps" / "egress-gateway" / "Dockerfile").read_text(
            encoding="utf-8"
        )
        self.assertIn("egress_entrypoint.py rtmp_session.py", dockerfile)
        self.assertIn(
            'CMD ["python3", "-u", "/app/egress_entrypoint.py"]',
            dockerfile,
        )


if __name__ == "__main__":
    unittest.main()
