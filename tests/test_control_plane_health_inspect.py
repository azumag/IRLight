from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CONTROL_API = ROOT / "apps" / "control-api"
if str(CONTROL_API) not in sys.path:
    sys.path.insert(0, str(CONTROL_API))

from control_plane_health_inspect_cli import (  # noqa: E402
    ControlPlaneProbeError,
    _bounded_timeout,
    _local_base_url,
    inspect_control_plane,
    main as health_inspect_main,
)


class ControlPlaneHealthInspectTest(unittest.TestCase):
    def test_base_url_accepts_loopback_only(self) -> None:
        self.assertEqual(
            _local_base_url("http://127.0.0.1:8080/"),
            "http://127.0.0.1:8080",
        )
        self.assertEqual(
            _local_base_url("https://localhost:8443"),
            "https://localhost:8443",
        )
        self.assertEqual(
            _local_base_url("http://[::1]:8080"),
            "http://[::1]:8080",
        )

        for value in (
            "http://169.254.169.254/latest/meta-data",
            "http://10.0.0.1:8080",
            "http://user:secret@127.0.0.1:8080",
            "http://127.0.0.1:8080/api",
            "http://127.0.0.1:8080?token=secret",
            "file:///etc/passwd",
        ):
            with self.subTest(value=value):
                with self.assertRaises(argparse.ArgumentTypeError):
                    _local_base_url(value)

    def test_timeout_is_positive_finite_and_bounded(self) -> None:
        self.assertEqual(_bounded_timeout("3"), 3.0)
        for value in ("0", "-1", "11", "nan", "inf"):
            with self.subTest(value=value):
                with self.assertRaises(argparse.ArgumentTypeError):
                    _bounded_timeout(value)

    def test_ready_when_liveness_and_readiness_are_successful(self) -> None:
        with patch(
            "control_plane_health_inspect_cli._probe_http_status",
            side_effect=[200, 204],
        ) as probe:
            code, payload = inspect_control_plane(
                "http://127.0.0.1:8080", timeout_seconds=3.0
            )
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "READY")
        self.assertEqual(payload["reason"], "CONTROL_PLANE_READY")
        self.assertEqual(probe.call_count, 2)

    def test_not_ready_keeps_liveness_distinct(self) -> None:
        with patch(
            "control_plane_health_inspect_cli._probe_http_status",
            side_effect=[200, 503],
        ):
            code, payload = inspect_control_plane(
                "http://127.0.0.1:8080", timeout_seconds=3.0
            )
        self.assertEqual(code, 2)
        self.assertEqual(payload["status"], "NOT_READY")
        self.assertEqual(payload["reason"], "READINESS_FAILED")
        self.assertEqual(payload["liveness_http_status"], 200)
        self.assertEqual(payload["readiness_http_status"], 503)

    def test_liveness_transport_failure_does_not_probe_readiness(self) -> None:
        with patch(
            "control_plane_health_inspect_cli._probe_http_status",
            side_effect=ControlPlaneProbeError("sensitive detail"),
        ) as probe:
            code, payload = inspect_control_plane(
                "http://127.0.0.1:8080", timeout_seconds=3.0
            )
        self.assertEqual(code, 3)
        self.assertEqual(payload["reason"], "CONTROL_PLANE_UNAVAILABLE")
        self.assertEqual(probe.call_count, 1)

    def test_cli_output_does_not_echo_transport_exception(self) -> None:
        output = io.StringIO()
        with patch(
            "control_plane_health_inspect_cli._probe_http_status",
            side_effect=ControlPlaneProbeError("AUDIT_DUMMY_SECRET"),
        ):
            with contextlib.redirect_stdout(output):
                code = health_inspect_main([])
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 3)
        self.assertEqual(payload["reason"], "CONTROL_PLANE_UNAVAILABLE")
        self.assertNotIn("AUDIT_DUMMY_SECRET", output.getvalue())
        self.assertNotIn("127.0.0.1", output.getvalue())


if __name__ == "__main__":
    unittest.main()
