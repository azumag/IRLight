from __future__ import annotations

import importlib.util
import json
import math
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "run-soak-sampling.py"
SPEC = importlib.util.spec_from_file_location("run_soak_sampling", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


SoakSamplingError = MODULE.SoakSamplingError


class FakeClock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


class SoakSamplingTest(unittest.TestCase):
    def test_sample_targets_include_baseline_and_exact_final_duration(self) -> None:
        self.assertEqual(MODULE.sample_targets(65.0, 30.0), [0.0, 30.0, 60.0, 65.0])
        self.assertEqual(MODULE.sample_targets(10.0, 30.0), [0.0, 10.0])
        self.assertEqual(MODULE.sample_targets(60.0, 30.0), [0.0, 30.0, 60.0])

    def test_sampling_arguments_reject_non_finite_and_non_positive_values(self) -> None:
        invalid = (True, 0.0, -1.0, math.inf, -math.inf, math.nan, 10**1000)
        for value in invalid:
            with self.subTest(value=repr(value)):
                with self.assertRaises(SoakSamplingError):
                    MODULE.sample_targets(value, 1.0)
                with self.assertRaises(SoakSamplingError):
                    MODULE.sample_targets(1.0, value)

    def test_project_name_is_limited_to_disposable_soak_namespace(self) -> None:
        self.assertEqual(
            MODULE.validate_project_name("irlight-poc-soak-ci-123"),
            "irlight-poc-soak-ci-123",
        )
        for project in ("irlight-poc", "production", "irlight-poc-soak-", "irlight-poc-soak-$HOME"):
            with self.subTest(project=project), self.assertRaises(SoakSamplingError):
                MODULE.validate_project_name(project)

    def test_build_collector_argv_requires_explicit_media_measurement_mode(self) -> None:
        common = {
            "collector_path": Path("scripts/collect-soak-resource-sample.py"),
            "project": "irlight-poc-soak-unit",
            "compose_file": Path("docker-compose.poc.yml"),
            "elapsed_seconds": 5.0,
        }
        with self.assertRaisesRegex(SoakSamplingError, "media-metrics-file"):
            MODULE.build_collector_argv(
                **common,
                media_metrics_file=None,
                allow_unmeasured_media=False,
            )

        measured = MODULE.build_collector_argv(
            **common,
            media_metrics_file=Path("/tmp/media.json"),
            allow_unmeasured_media=False,
        )
        self.assertIn("--media-metrics-file", measured)
        self.assertNotIn("--allow-unmeasured-media", measured)

        unmeasured = MODULE.build_collector_argv(
            **common,
            media_metrics_file=None,
            allow_unmeasured_media=True,
        )
        self.assertIn("--allow-unmeasured-media", unmeasured)
        self.assertNotIn("--media-metrics-file", unmeasured)

    def test_collector_output_is_single_strict_json_object_with_matching_elapsed(self) -> None:
        sample = MODULE.parse_collector_output(
            '{"elapsed_seconds":5.0,"memory_rss_bytes":1}\n',
            expected_elapsed=5.0,
        )
        self.assertEqual(sample["elapsed_seconds"], 5.0)

        invalid_outputs = (
            '{"elapsed_seconds":5,"elapsed_seconds":5}\n',
            '{"elapsed_seconds":NaN}\n',
            '{"elapsed_seconds":4}\n',
            '{"elapsed_seconds":5}\n{"elapsed_seconds":5}\n',
            '[]\n',
        )
        for output in invalid_outputs:
            with self.subTest(output=output), self.assertRaises(SoakSamplingError):
                MODULE.parse_collector_output(output, expected_elapsed=5.0)

    def test_collector_timeout_and_failure_are_fail_closed(self) -> None:
        with mock.patch.object(
            MODULE.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd=["python"], timeout=1.0),
        ):
            with self.assertRaisesRegex(SoakSamplingError, "bounded execution time"):
                MODULE.collect_one_process(
                    ["python", "collector.py"],
                    expected_elapsed=0.0,
                    timeout_seconds=1.0,
                )

        completed = subprocess.CompletedProcess(
            ["python", "collector.py"],
            2,
            stdout="",
            stderr="synthetic failure",
        )
        with mock.patch.object(MODULE.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(SoakSamplingError, "synthetic failure"):
                MODULE.collect_one_process(
                    ["python", "collector.py"],
                    expected_elapsed=0.0,
                    timeout_seconds=1.0,
                )

    def test_run_sampling_records_actual_elapsed_and_durable_jsonl(self) -> None:
        clock = FakeClock()
        collected: list[float] = []

        def collect(elapsed: float) -> dict[str, object]:
            collected.append(elapsed)
            return {
                "elapsed_seconds": elapsed,
                "memory_rss_bytes": 1,
            }

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "samples.jsonl")
            with MODULE.open_output_exclusive(path) as output:
                count = MODULE.run_sampling(
                    duration_seconds=65.0,
                    interval_seconds=30.0,
                    collect=collect,
                    output=output,
                    monotonic=clock.monotonic,
                    sleeper=clock.sleep,
                )

            self.assertEqual(count, 4)
            self.assertEqual(collected, [0.0, 30.0, 60.0, 65.0])
            self.assertEqual(clock.sleeps, [30.0, 30.0, 5.0])
            lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([line["elapsed_seconds"] for line in lines], collected)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_runner_records_delay_instead_of_claiming_nominal_schedule(self) -> None:
        clock = FakeClock()
        samples: list[float] = []

        def delayed_sleep(seconds: float) -> None:
            clock.value += seconds + 2.5

        def collect(elapsed: float) -> dict[str, object]:
            samples.append(elapsed)
            return {"elapsed_seconds": elapsed}

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "samples.jsonl")
            with MODULE.open_output_exclusive(path) as output:
                MODULE.run_sampling(
                    duration_seconds=10.0,
                    interval_seconds=5.0,
                    collect=collect,
                    output=output,
                    monotonic=clock.monotonic,
                    sleeper=delayed_sleep,
                )

        self.assertEqual(samples[0], 0.0)
        self.assertEqual(samples[1], 7.5)
        self.assertEqual(samples[2], 12.5)

    def test_existing_output_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "samples.jsonl")
            path.write_text("original\n", encoding="utf-8")
            with self.assertRaisesRegex(SoakSamplingError, "refusing to overwrite"):
                MODULE.open_output_exclusive(path)
            self.assertEqual(path.read_text(encoding="utf-8"), "original\n")

    def test_backward_monotonic_clock_fails_closed(self) -> None:
        values = iter((100.0, 99.0))
        with self.assertRaisesRegex(SoakSamplingError, "moved backwards"):
            MODULE.wait_until(
                101.0,
                started_at=100.0,
                monotonic=lambda: next(values),
                sleeper=lambda _: None,
            )


if __name__ == "__main__":
    unittest.main()
