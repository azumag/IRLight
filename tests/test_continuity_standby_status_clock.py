from __future__ import annotations

import importlib.util
import math
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "apps" / "continuity" / "runner.py"


def _load_runner():
    fake_continuity = types.ModuleType("continuity")

    class ContinuityPipeline:
        pass

    fake_continuity.ContinuityPipeline = ContinuityPipeline
    fake_continuity.atomic_write_json = lambda path, payload: None

    fake_runtime_timer_config = types.ModuleType("runtime_timer_config")
    fake_runtime_timer_config.finite_env_float = (
        lambda name, default: float(default)
    )

    fake_standby_asset = types.ModuleType("standby_asset")
    fake_standby_asset.NODE_DEFAULT_IMAGE_PATH = "/unused/default.png"
    fake_standby_asset.gst_standby_source = lambda selection: "unused"
    fake_standby_asset.public_standby_status = lambda selection: {
        "source": "SYNTHETIC_BLACK",
        "fallback_reason": None,
        "custom_configured": False,
    }

    fake_standby_integrity = types.ModuleType("standby_integrity")
    fake_standby_integrity.resolve_integrity_checked_standby_asset = (
        lambda *args, **kwargs: object()
    )

    spec = importlib.util.spec_from_file_location(
        "continuity_runner_standby_clock_test", RUNNER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with patch.dict(
        sys.modules,
        {
            "continuity": fake_continuity,
            "runtime_timer_config": fake_runtime_timer_config,
            "standby_asset": fake_standby_asset,
            "standby_integrity": fake_standby_integrity,
        },
    ):
        spec.loader.exec_module(module)
    return module


MODULE = _load_runner()


class ContinuityStandbyStatusClockTest(unittest.TestCase):
    def _pipeline_for(self, status_path: Path):
        pipeline = object.__new__(MODULE.StandbyAwareContinuityPipeline)
        pipeline.standby_status_path = status_path
        pipeline.standby_selection = object()
        return pipeline

    def test_invalid_clock_fails_closed_before_status_write(self) -> None:
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

        with tempfile.TemporaryDirectory(
            prefix="irlight-continuity-standby-clock-"
        ) as directory:
            root = Path(directory)
            status_path = root / "standby.json"
            original = b'{"source":"CUSTOM","selected_at":123.0}\n'
            status_path.write_bytes(original)
            pipeline = self._pipeline_for(status_path)

            for invalid in invalid_values:
                with self.subTest(invalid_type=type(invalid).__name__):
                    before_stat = status_path.stat()
                    before_entries = sorted(path.name for path in root.iterdir())

                    with patch.object(MODULE.time, "time", return_value=invalid):
                        with patch.object(MODULE, "atomic_write_json") as writer:
                            with self.assertRaisesRegex(
                                RuntimeError,
                                r"^standby status clock is invalid$",
                            ):
                                pipeline._write_standby_status()

                    writer.assert_not_called()
                    self.assertEqual(status_path.read_bytes(), original)
                    self.assertEqual(
                        status_path.stat().st_mtime_ns,
                        before_stat.st_mtime_ns,
                    )
                    self.assertEqual(
                        sorted(path.name for path in root.iterdir()),
                        before_entries,
                    )

    def test_epoch_zero_is_preserved_as_valid_selected_at(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="irlight-continuity-standby-clock-zero-"
        ) as directory:
            status_path = Path(directory) / "standby.json"
            pipeline = self._pipeline_for(status_path)

            with patch.object(MODULE.time, "time", return_value=0.0):
                with patch.object(MODULE, "atomic_write_json") as writer:
                    pipeline._write_standby_status()

            writer.assert_called_once_with(
                status_path,
                {
                    "source": "SYNTHETIC_BLACK",
                    "fallback_reason": None,
                    "custom_configured": False,
                    "selected_at": 0.0,
                },
            )

    def test_finite_positive_clock_is_normalized_to_float(self) -> None:
        self.assertEqual(MODULE._validated_standby_status_time(123), 123.0)
        self.assertEqual(MODULE._validated_standby_status_time(123.5), 123.5)


if __name__ == "__main__":
    unittest.main()
