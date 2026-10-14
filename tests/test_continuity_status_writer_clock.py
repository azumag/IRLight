from __future__ import annotations

import importlib.util
import json
import math
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CONTINUITY_DIR = ROOT / "apps" / "continuity"
sys.path.insert(0, str(CONTINUITY_DIR))


def _load_continuity_module():
    fake_gi = types.ModuleType("gi")
    fake_gi.require_version = lambda *_args, **_kwargs: None
    fake_repository = types.ModuleType("gi.repository")
    fake_repository.GLib = SimpleNamespace(MainLoop=object)
    fake_repository.Gst = SimpleNamespace(
        Element=object,
        Pad=object,
        Pipeline=object,
        init=lambda *_args: None,
        parse_launch=lambda *_args: None,
    )
    fake_repository.GstApp = SimpleNamespace()

    with mock.patch.dict(
        sys.modules,
        {
            "gi": fake_gi,
            "gi.repository": fake_repository,
        },
    ):
        spec = importlib.util.spec_from_file_location(
            "irlight_continuity_status_clock_target",
            CONTINUITY_DIR / "continuity.py",
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(spec.name, None)
        return module


CONTINUITY = _load_continuity_module()


class ContinuityStatusWriterClockTest(unittest.TestCase):
    def _pipeline(self, status_path: Path):
        pipeline = object.__new__(CONTINUITY.ContinuityPipeline)
        pipeline.status_path = status_path
        pipeline.profile = "PASSTHROUGH"
        pipeline.started_at = 100.0
        pipeline.last_control_version = 3
        pipeline.last_command_id = None
        pipeline.source_generation = 1
        pipeline.last_error = None
        pipeline.control_error = None
        pipeline.egress_url = "rtmp://mediamtx:1935/output/relay"
        pipeline._input_pipeline = None

        selector = SimpleNamespace(
            get_property=lambda _name: None,
            set_property=lambda *_args: None,
        )
        pipeline.video_selector = selector
        pipeline.audio_selector = selector
        pipeline.fallback_video_pad = object()
        pipeline.fallback_audio_pad = object()
        pipeline.live_video_pad = object()
        pipeline.live_audio_pad = object()

        pipeline.control_reader = SimpleNamespace(
            read=lambda: (
                SimpleNamespace(
                    audio_mode="LIVE",
                    version=3,
                    command_id=None,
                ),
                None,
            )
        )
        pipeline.model = CONTINUITY.ContinuityState(
            input_timeout=1.5,
            stable_window=3.0,
        )
        return pipeline

    def _assert_preserved_after_failure(
        self,
        status_path: Path,
        action,
        *,
        message: str,
    ) -> None:
        original = b'{\"session_status\":\"LIVE\",\"updated_at\":99.0}\n'
        status_path.write_bytes(original)
        before_stat = status_path.stat()
        before_entries = sorted(path.name for path in status_path.parent.iterdir())

        with self.assertRaisesRegex(
            CONTINUITY.RuntimeStatusWriteError,
            message,
        ):
            action()

        self.assertEqual(status_path.read_bytes(), original)
        self.assertEqual(status_path.stat().st_mtime_ns, before_stat.st_mtime_ns)
        self.assertEqual(
            sorted(path.name for path in status_path.parent.iterdir()),
            before_entries,
        )

    def test_invalid_wall_clock_preserves_previous_status(self) -> None:
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
                    prefix="irlight-continuity-wall-clock-"
                ) as directory:
                    path = Path(directory) / "status.json"
                    pipeline = self._pipeline(path)
                    with mock.patch.object(
                        CONTINUITY.time,
                        "time",
                        return_value=invalid,
                    ):
                        self._assert_preserved_after_failure(
                            path,
                            pipeline._reconcile,
                            message=r"^continuity updated_at is invalid$",
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
                    prefix="irlight-continuity-started-clock-"
                ) as directory:
                    path = Path(directory) / "status.json"
                    pipeline = self._pipeline(path)
                    pipeline.started_at = invalid
                    with mock.patch.object(
                        CONTINUITY.time,
                        "time",
                        return_value=101.0,
                    ):
                        self._assert_preserved_after_failure(
                            path,
                            pipeline._reconcile,
                            message=r"^continuity started_at is invalid$",
                        )

    def test_epoch_zero_is_valid_for_both_persisted_timestamps(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="irlight-continuity-clock-zero-"
        ) as directory:
            path = Path(directory) / "status.json"
            pipeline = self._pipeline(path)
            pipeline.started_at = 0

            with mock.patch.object(CONTINUITY.time, "time", return_value=0):
                self.assertTrue(pipeline._reconcile())

            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["started_at"], 0.0)
            self.assertEqual(payload["updated_at"], 0.0)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*")), [])

    def test_valid_status_payload_shape_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="irlight-continuity-status-shape-"
        ) as directory:
            path = Path(directory) / "status.json"
            pipeline = self._pipeline(path)

            with mock.patch.object(CONTINUITY.time, "time", return_value=1234.5):
                with mock.patch.object(
                    CONTINUITY.time,
                    "monotonic",
                    return_value=99.0,
                ):
                    self.assertTrue(pipeline._reconcile())

            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["profile"], "PASSTHROUGH")
            self.assertEqual(payload["session_status"], "HOLDING")
            self.assertEqual(payload["video_source"], "STANDBY")
            self.assertEqual(payload["actual_audio_mode"], "SILENT_FALLBACK")
            self.assertEqual(payload["started_at"], 100.0)
            self.assertEqual(payload["updated_at"], 1234.5)


if __name__ == "__main__":
    unittest.main()
