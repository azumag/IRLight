from __future__ import annotations

import importlib.util
import json
import math
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
EGRESS_DIR = ROOT / "apps" / "egress-gateway"
sys.path.insert(0, str(EGRESS_DIR))


def _load_egress_module():
    fake_gi = types.ModuleType("gi")
    fake_gi.require_version = lambda *_args, **_kwargs: None
    fake_repository = types.ModuleType("gi.repository")
    fake_repository.GLib = types.SimpleNamespace()
    fake_repository.Gst = types.SimpleNamespace()

    with mock.patch.dict(
        sys.modules,
        {
            "gi": fake_gi,
            "gi.repository": fake_repository,
        },
    ):
        spec = importlib.util.spec_from_file_location(
            "irlight_egress_status_clock_target",
            EGRESS_DIR / "egress.py",
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("failed to load egress module")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(spec.name, None)
        return module


EGRESS = _load_egress_module()


class EgressStatusWriterClockTest(unittest.TestCase):
    def _gateway(self, status_path: Path):
        gateway = object.__new__(EGRESS.EgressGateway)
        gateway.status_file = status_path
        gateway.failure_count = 0
        gateway.destination_scheme = "rtmps"
        gateway.destination_host = "live.example"
        gateway.started_at = 100.0
        return gateway

    def _assert_preserved_after_failure(
        self,
        status_path: Path,
        action,
        *,
        message: str,
    ) -> None:
        original = b'{"status":"CONNECTED","observed_at":99.0}\n'
        status_path.write_bytes(original)
        before_stat = status_path.stat()
        before_entries = sorted(path.name for path in status_path.parent.iterdir())

        with self.assertRaisesRegex(EGRESS.RuntimeStatusWriteError, message):
            action()

        self.assertEqual(status_path.read_bytes(), original)
        self.assertEqual(status_path.stat().st_mtime_ns, before_stat.st_mtime_ns)
        self.assertEqual(
            sorted(path.name for path in status_path.parent.iterdir()),
            before_entries,
        )

    def test_invalid_observed_clock_preserves_previous_status(self) -> None:
        invalid_values = (
            -1.0,
            math.nan,
            math.inf,
            -math.inf,
            True,
            None,
            "1",
            10**10000,
        )
        for invalid in invalid_values:
            with self.subTest(invalid_type=type(invalid).__name__):
                with tempfile.TemporaryDirectory(
                    prefix="irlight-egress-observed-clock-"
                ) as directory:
                    path = Path(directory) / "egress.json"
                    gateway = self._gateway(path)
                    with mock.patch.object(
                        EGRESS.time,
                        "time",
                        return_value=invalid,
                    ):
                        self._assert_preserved_after_failure(
                            path,
                            lambda: gateway._write_status(
                                "CONNECTED",
                                connected=True,
                            ),
                            message=r"^egress observed_at is invalid$",
                        )

    def test_invalid_started_at_preserves_previous_status(self) -> None:
        invalid_values = (
            -1.0,
            math.nan,
            math.inf,
            -math.inf,
            True,
            None,
            "1",
            10**10000,
        )
        for invalid in invalid_values:
            with self.subTest(invalid_type=type(invalid).__name__):
                with tempfile.TemporaryDirectory(
                    prefix="irlight-egress-started-clock-"
                ) as directory:
                    path = Path(directory) / "egress.json"
                    gateway = self._gateway(path)
                    gateway.started_at = invalid
                    with mock.patch.object(EGRESS.time, "time", return_value=101.0):
                        self._assert_preserved_after_failure(
                            path,
                            lambda: gateway._write_status(
                                "CONNECTED",
                                connected=True,
                            ),
                            message=r"^egress started_at is invalid$",
                        )

    def test_invalid_next_retry_at_preserves_previous_status(self) -> None:
        invalid_values = (
            -1.0,
            math.nan,
            math.inf,
            -math.inf,
            True,
            "1",
            10**10000,
        )
        for invalid in invalid_values:
            with self.subTest(invalid_type=type(invalid).__name__):
                with tempfile.TemporaryDirectory(
                    prefix="irlight-egress-retry-clock-"
                ) as directory:
                    path = Path(directory) / "egress.json"
                    gateway = self._gateway(path)
                    with mock.patch.object(EGRESS.time, "time", return_value=101.0):
                        self._assert_preserved_after_failure(
                            path,
                            lambda: gateway._write_status(
                                "RECONNECTING",
                                connected=False,
                                next_retry_at=invalid,
                            ),
                            message=r"^egress next_retry_at is invalid$",
                        )

    def test_epoch_zero_is_valid_for_all_persisted_timestamps(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="irlight-egress-clock-zero-"
        ) as directory:
            path = Path(directory) / "egress.json"
            gateway = self._gateway(path)
            gateway.started_at = 0

            with mock.patch.object(EGRESS.time, "time", return_value=0):
                gateway._write_status(
                    "RECONNECTING",
                    connected=False,
                    next_retry_at=0,
                )

            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["started_at"], 0.0)
            self.assertEqual(payload["observed_at"], 0.0)
            self.assertEqual(payload["next_retry_at"], 0.0)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*")), [])

    def test_constructor_rejects_invalid_startup_clock(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(
                EGRESS,
                "_read_input_uri",
                return_value="rtsp://example.invalid/live",
            ):
                with mock.patch.object(EGRESS.time, "time", return_value=-1.0):
                    with self.assertRaisesRegex(
                        EGRESS.RuntimeStatusWriteError,
                        r"^egress started_at is invalid$",
                    ):
                        EGRESS.EgressGateway()


if __name__ == "__main__":
    unittest.main()
