from __future__ import annotations

import ast
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

    def test_timeout_registration_uses_guarded_callback(self) -> None:
        tree = ast.parse((CONTINUITY_DIR / "continuity.py").read_text())
        pipeline = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "ContinuityPipeline"
        )
        run = next(
            node for node in pipeline.body
            if isinstance(node, ast.FunctionDef) and node.name == "run"
        )
        registrations = [
            node for node in ast.walk(run)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and ast.unparse(node.func) == "GLib.timeout_add"
        ]
        self.assertEqual(len(registrations), 1)
        self.assertEqual(ast.unparse(registrations[0].args[0]), "250")
        self.assertEqual(
            ast.unparse(registrations[0].args[1]), "self._reconcile_timeout"
        )

    def test_validated_startup_clock_rejects_invalid_and_accepts_zero(self) -> None:
        invalid_values = (
            -1.0, math.nan, math.inf, -math.inf, True, None, "1", 10**10000,
        )
        for invalid in invalid_values:
            with self.subTest(invalid_type=type(invalid).__name__):
                with mock.patch.object(CONTINUITY.time, "time", return_value=invalid):
                    with self.assertRaisesRegex(
                        CONTINUITY.RuntimeStatusWriteError,
                        r"^continuity started_at is invalid$",
                    ):
                        CONTINUITY._validated_status_now(field="continuity started_at")
        for zero in (0, 0.0, -0.0):
            with mock.patch.object(CONTINUITY.time, "time", return_value=zero):
                self.assertEqual(
                    CONTINUITY._validated_status_now(field="continuity started_at"),
                    0.0,
                )

    def test_timeout_keeps_source_on_invalid_status_clocks(self) -> None:
        invalid_values = (
            -1.0, math.nan, math.inf, -math.inf, True, None, "1", 10**10000,
        )
        for field in ("started_at", "updated_at"):
            for invalid in invalid_values:
                with self.subTest(field=field, invalid_type=type(invalid).__name__):
                    pipeline = self._pipeline(Path("unused-status.json"))
                    wall = invalid if field == "updated_at" else 101.0
                    if field == "started_at":
                        pipeline.started_at = invalid
                    with (
                        mock.patch.object(CONTINUITY.time, "time", return_value=wall),
                        mock.patch.object(
                            CONTINUITY.time, "monotonic", return_value=10.0,
                        ),
                        mock.patch.object(CONTINUITY, "atomic_write_json") as writer,
                        mock.patch.object(CONTINUITY.LOG, "warning") as warning,
                        mock.patch.object(pipeline, "_activate_pad") as activate,
                    ):
                        self.assertTrue(pipeline._reconcile_timeout())
                        writer.assert_not_called()
                        self.assertEqual(activate.call_count, 2)
                        warning.assert_called_once_with(
                            "Skipping invalid runtime status update"
                        )

    def test_timeout_keeps_switching_and_recovers_status(self) -> None:
        pipeline = self._pipeline(Path("unused-status.json"))
        pipeline.model = CONTINUITY.ContinuityState(
            input_timeout=1.5, stable_window=0.0,
        )
        control = SimpleNamespace(audio_mode="LIVE", version=3, command_id=None)
        pipeline.control_reader.read = lambda: (control, None)
        with (
            mock.patch.object(
                CONTINUITY.time, "time", side_effect=[-1.0, math.nan, math.inf, 101.0],
            ),
            mock.patch.object(
                CONTINUITY.time, "monotonic", side_effect=[10.0, 12.0, 13.0, 14.0],
            ),
            mock.patch.object(CONTINUITY, "atomic_write_json") as writer,
            mock.patch.object(CONTINUITY.LOG, "warning"),
            mock.patch.object(pipeline, "_activate_pad") as activate,
        ):
            pipeline.model.observe_video(10.0)
            pipeline.model.observe_audio(10.0)
            self.assertTrue(pipeline._reconcile_timeout())
            writer.assert_not_called()
            self.assertTrue(pipeline._reconcile_timeout())
            writer.assert_not_called()
            control.audio_mode = "MUTED"
            pipeline.model.observe_video(13.0)
            pipeline.model.observe_audio(13.0)
            self.assertTrue(pipeline._reconcile_timeout())
            writer.assert_not_called()
            control.audio_mode = "LIVE"
            pipeline.model.observe_video(14.0)
            pipeline.model.observe_audio(14.0)
            self.assertTrue(pipeline._reconcile_timeout())
            self.assertEqual(activate.call_args_list, [
                mock.call(pipeline.video_selector, pipeline.live_video_pad),
                mock.call(pipeline.audio_selector, pipeline.live_audio_pad),
                mock.call(pipeline.video_selector, pipeline.fallback_video_pad),
                mock.call(pipeline.audio_selector, pipeline.fallback_audio_pad),
                mock.call(pipeline.video_selector, pipeline.live_video_pad),
                mock.call(pipeline.audio_selector, pipeline.fallback_audio_pad),
                mock.call(pipeline.video_selector, pipeline.live_video_pad),
                mock.call(pipeline.audio_selector, pipeline.live_audio_pad),
            ])
            writer.assert_called_once()
            payload = writer.call_args.args[1]
            normalized = json.loads(json.dumps(payload, allow_nan=False))
            self.assertEqual(normalized["updated_at"], 101.0)
            self.assertEqual(payload["actual_audio_mode"], "LIVE")

    def test_timeout_only_handles_status_errors(self) -> None:
        pipeline = self._pipeline(Path("unused-status.json"))
        with (
            mock.patch.object(
                pipeline, "_reconcile",
                side_effect=CONTINUITY.RuntimeStatusWriteError(
                    "runtime status payload is not strict JSON"
                ),
            ),
            mock.patch.object(CONTINUITY.LOG, "warning") as warning,
        ):
            self.assertTrue(pipeline._reconcile_timeout())
            warning.assert_called_once_with("Skipping invalid runtime status update")
        errors = (RuntimeError("unexpected"), OSError("write failed"), ValueError("control"))
        for error in errors:
            with self.subTest(error_type=type(error).__name__):
                with mock.patch.object(pipeline, "_reconcile", side_effect=error):
                    with self.assertRaises(type(error)) as raised:
                        pipeline._reconcile_timeout()
                    self.assertIs(raised.exception, error)
        with mock.patch.object(pipeline, "_reconcile", return_value=False):
            self.assertFalse(pipeline._reconcile_timeout())


if __name__ == "__main__":
    unittest.main()
