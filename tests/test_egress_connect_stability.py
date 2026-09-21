from __future__ import annotations

import importlib.util
import sys
import threading
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
            "irlight_egress_connect_stability_target",
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


class _Stats:
    def __init__(self, values: dict[str, object]) -> None:
        self.values = values

    def get_value(self, key: str) -> object:
        return self.values.get(key)


class _Sink:
    def __init__(self, values: dict[str, object]) -> None:
        self.stats = _Stats(values)

    def get_property(self, name: str) -> _Stats:
        if name != "stats":
            raise ValueError(name)
        return self.stats


class EgressConnectStabilityTest(unittest.TestCase):
    def _attempt(self, sink_factory: str):
        attempt = object.__new__(EGRESS.EgressAttempt)
        attempt.stop_event = threading.Event()
        attempt.connected_once = False
        attempt.rendered_buffers = 0
        attempt.connect_timeout_seconds = 0.0
        attempt.status_heartbeat_seconds = 5.0
        attempt.connect_stability_seconds = 3.0
        attempt.output_stall_timeout_seconds = 5.0
        attempt.sink_factory = sink_factory
        attempt.sink = _Sink({})
        attempt._observed_sink_buffers = 0
        attempt._attempt_started = 0.0
        attempt._first_rendered_at = None
        attempt._last_progress_at = 0.0
        attempt._last_reported_rendered = 0
        attempt._last_progress_marker = (0, 0)
        attempt._last_rendered_at = 0.0
        attempt.on_connected = None
        attempt.on_progress = None
        attempt.loop = types.SimpleNamespace(quit=lambda: None)
        return attempt

    def _set_ready(self, attempt, counter: int) -> None:
        if attempt.sink_factory == "rtmp2sink":
            attempt.sink.stats.values = {
                "out-bytes-total": counter * 1024,
                "out-bytes-acked": counter * 512,
            }
            attempt._observed_sink_buffers = counter
        else:
            attempt.sink.stats.values = {"rendered": counter}

    def _set_not_ready(self, attempt) -> None:
        if attempt.sink_factory == "rtmp2sink":
            # A malformed transport counter is deliberately fail-closed to zero.
            attempt.sink.stats.values = {
                "out-bytes-total": None,
                "out-bytes-acked": 512,
            }
        else:
            attempt.sink.stats.values = {"rendered": None}

    def test_ready_loss_restarts_stability_window_for_both_sinks(self) -> None:
        for sink_factory in ("rtmpsink", "rtmp2sink"):
            with self.subTest(sink_factory=sink_factory):
                attempt = self._attempt(sink_factory)

                self._set_ready(attempt, 1)
                with mock.patch.object(EGRESS.time, "monotonic", return_value=0.0):
                    self.assertTrue(attempt._poll_sink())
                self.assertFalse(attempt.connected_once)
                self.assertEqual(attempt._first_rendered_at, 0.0)

                self._set_not_ready(attempt)
                with mock.patch.object(EGRESS.time, "monotonic", return_value=2.0):
                    self.assertTrue(attempt._poll_sink())
                self.assertFalse(attempt.connected_once)
                self.assertIsNone(attempt._first_rendered_at)

                self._set_ready(attempt, 2)
                with mock.patch.object(EGRESS.time, "monotonic", return_value=4.0):
                    self.assertTrue(attempt._poll_sink())
                self.assertFalse(attempt.connected_once)
                self.assertEqual(attempt._first_rendered_at, 4.0)

                self._set_ready(attempt, 3)
                with mock.patch.object(EGRESS.time, "monotonic", return_value=7.1):
                    self.assertTrue(attempt._poll_sink())
                self.assertTrue(attempt.connected_once)


if __name__ == "__main__":
    unittest.main()
