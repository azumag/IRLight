from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "node-agent"))

from ingest_failure_inspect_cli import (  # noqa: E402
    IngestFailureInspectError,
    main as ingest_failure_inspect_main,
    summarize_paths,
)


class IngestFailureSummaryTest(unittest.TestCase):
    def test_online_publisher_is_redacted(self) -> None:
        payload = {
            "items": [
                {
                    "name": "live/input",
                    "online": True,
                    "source": {
                        "type": "rtmpsConn",
                        "id": "super-secret-source-id",
                        "url": "rtmps://example.invalid/live/stream-key",
                    },
                    "tracks2": [
                        {"codec": "H264", "private": "secret-video"},
                        {"codec": "MPEG-4 Audio", "private": "secret-audio"},
                    ],
                }
            ]
        }

        result = summarize_paths(payload, ingest_path="live/input")

        self.assertEqual(
            result,
            {
                "status": "ONLINE",
                "reason": "INGEST_PUBLISHER_ONLINE",
                "online": True,
                "source_protocol": "RTMPS",
                "track_count": 2,
            },
        )
        rendered = json.dumps(result)
        self.assertNotIn("super-secret-source-id", rendered)
        self.assertNotIn("stream-key", rendered)
        self.assertNotIn("secret-video", rendered)

    def test_offline_visible_path_reports_no_publisher(self) -> None:
        result = summarize_paths(
            {"items": [{"name": "live/input", "online": False}]},
            ingest_path="live/input",
        )
        self.assertEqual(result["status"], "OFFLINE")
        self.assertEqual(result["reason"], "INGEST_NO_PUBLISHER")
        self.assertFalse(result["online"])

    def test_missing_path_is_distinct_from_visible_offline_path(self) -> None:
        result = summarize_paths(
            {"items": [{"name": "output/relay", "online": True}]},
            ingest_path="live/input",
        )
        self.assertEqual(result["reason"], "INGEST_PATH_NOT_VISIBLE")
        self.assertFalse(result["online"])

    def test_unknown_source_type_is_not_echoed(self) -> None:
        result = summarize_paths(
            {
                "items": [
                    {
                        "name": "live/input",
                        "online": True,
                        "source": {"type": "futureConn-secret-detail"},
                        "tracks2": [],
                    }
                ]
            },
            ingest_path="live/input",
        )
        self.assertEqual(result["source_protocol"], "OTHER")
        self.assertNotIn("futureConn", json.dumps(result))

    def test_malformed_path_api_fails_closed(self) -> None:
        for payload in (
            {},
            {"items": {}},
            {"items": [1]},
            {"items": [{"name": "live/input"}]},
            {"items": [{"name": "live/input", "online": True, "source": None}]},
            {
                "items": [
                    {
                        "name": "live/input",
                        "online": True,
                        "source": {"type": "rtmpConn"},
                        "tracks2": {},
                    }
                ]
            },
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(IngestFailureInspectError):
                    summarize_paths(payload, ingest_path="live/input")


class IngestFailureCliTest(unittest.TestCase):
    def test_offline_cli_uses_problem_exit_code(self) -> None:
        output = io.StringIO()
        with patch.dict(
            os.environ,
            {
                "NODE_MEDIAMTX_API_URL": "http://mediamtx:9997",
                "NODE_INGEST_PATH": "live/input",
            },
            clear=True,
        ):
            with patch(
                "ingest_failure_inspect_cli.inspect_ingest",
                return_value={
                    "status": "OFFLINE",
                    "reason": "INGEST_NO_PUBLISHER",
                    "online": False,
                    "source_protocol": None,
                    "track_count": 0,
                },
            ):
                with contextlib.redirect_stdout(output):
                    result = ingest_failure_inspect_main([])
        self.assertEqual(result, 2)
        self.assertEqual(json.loads(output.getvalue())["reason"], "INGEST_NO_PUBLISHER")

    def test_unavailable_cli_does_not_echo_internal_error_or_api_url(self) -> None:
        output = io.StringIO()
        secret_url = "http://user:super-secret@mediamtx:9997"
        with patch.dict(
            os.environ,
            {
                "NODE_MEDIAMTX_API_URL": secret_url,
                "NODE_INGEST_PATH": "live/input",
            },
            clear=True,
        ):
            with patch(
                "ingest_failure_inspect_cli.inspect_ingest",
                side_effect=IngestFailureInspectError("secret-from-api-body"),
            ):
                with contextlib.redirect_stdout(output):
                    result = ingest_failure_inspect_main([])

        self.assertEqual(result, 3)
        rendered = output.getvalue()
        self.assertEqual(
            json.loads(rendered),
            {"status": "UNAVAILABLE", "reason": "INGEST_INSPECTION_UNAVAILABLE"},
        )
        self.assertNotIn("super-secret", rendered)
        self.assertNotIn("secret-from-api-body", rendered)

    def test_timeout_is_strictly_bounded(self) -> None:
        for value in ("0.1", "11", "nan", "inf"):
            with self.subTest(value=value):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as raised:
                        ingest_failure_inspect_main(["--timeout-seconds", value])
                self.assertEqual(raised.exception.code, 2)
                self.assertIn("between 0.2 and 10 seconds", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
