from __future__ import annotations

import importlib.util
import json
import math
import stat
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "run-soak-sampling.py"
SPEC = importlib.util.spec_from_file_location("run_soak_sampling", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


SoakSamplingError = MODULE.SoakSamplingError


def sample(elapsed: float, **changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "elapsed_seconds": elapsed,
        "memory_rss_bytes": 1,
        "cpu_percent": 2.0,
        "open_fds": 3,
        "processes": 4,
        "zombies": 0,
        "bitrate_bps": 5_000_000.0,
        "av_sync_drift_ms": -2.5,
        "timestamp_errors": 0,
        "unexpected_reconnects": 0,
    }
    value.update(changes)
    return value


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
    def test_next_target_skips_missed_intervals_and_targets_duration(self) -> None:
        self.assertEqual(
            MODULE.next_sampling_target(
                elapsed_seconds=0.0,
                duration_seconds=65.0,
                interval_seconds=30.0,
            ),
            30.0,
        )
        self.assertEqual(
            MODULE.next_sampling_target(
                elapsed_seconds=30.0,
                duration_seconds=65.0,
                interval_seconds=30.0,
            ),
            60.0,
        )
        self.assertEqual(
            MODULE.next_sampling_target(
                elapsed_seconds=61.0,
                duration_seconds=65.0,
                interval_seconds=30.0,
            ),
            65.0,
        )
        self.assertEqual(
            MODULE.next_sampling_target(
                elapsed_seconds=7.5,
                duration_seconds=10.0,
                interval_seconds=5.0,
            ),
            10.0,
        )
        self.assertIsNone(
            MODULE.next_sampling_target(
                elapsed_seconds=65.0,
                duration_seconds=65.0,
                interval_seconds=30.0,
            )
        )

    def test_sampling_arguments_reject_non_finite_and_non_positive_values(self) -> None:
        invalid = (True, 0.0, -1.0, math.inf, -math.inf, math.nan, 10**1000)
        for value in invalid:
            with self.subTest(value=repr(value)):
                with self.assertRaises(SoakSamplingError):
                    MODULE.next_sampling_target(
                        elapsed_seconds=0.0,
                        duration_seconds=value,
                        interval_seconds=1.0,
                    )
                with self.assertRaises(SoakSamplingError):
                    MODULE.next_sampling_target(
                        elapsed_seconds=0.0,
                        duration_seconds=1.0,
                        interval_seconds=value,
                    )

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

    def test_collector_output_is_single_strict_schema_object_with_matching_elapsed(self) -> None:
        expected = sample(5.0)
        parsed = MODULE.parse_collector_output(
            json.dumps(expected) + "\n",
            expected_elapsed=5.0,
        )
        self.assertEqual(parsed, expected)

        duplicate = json.dumps(expected).replace(
            '"elapsed_seconds": 5.0',
            '"elapsed_seconds": 5.0, "elapsed_seconds": 5.0',
            1,
        )
        non_finite = json.dumps(expected).replace(
            '"cpu_percent": 2.0', '"cpu_percent": NaN', 1
        )
        wrong_elapsed = dict(expected)
        wrong_elapsed["elapsed_seconds"] = 4.0
        missing = dict(expected)
        missing.pop("open_fds")
        extra = dict(expected)
        extra["unexpected"] = 1
        bad_boolean = dict(expected)
        bad_boolean["timestamp_errors"] = False
        too_many_zombies = dict(expected)
        too_many_zombies["zombies"] = 5

        invalid_outputs = (
            duplicate + "\n",
            non_finite + "\n",
            json.dumps(wrong_elapsed) + "\n",
            json.dumps(expected) + "\n" + json.dumps(expected) + "\n",
            "[]\n",
            json.dumps(missing) + "\n",
            json.dumps(extra) + "\n",
            json.dumps(bad_boolean) + "\n",
            json.dumps(too_many_zombies) + "\n",
        )
        for output in invalid_outputs:
            with self.subTest(output=output[:80]), self.assertRaises(SoakSamplingError):
                MODULE.parse_collector_output(output, expected_elapsed=5.0)

    def test_collector_timeout_and_failure_are_fail_closed(self) -> None:
        with self.assertRaisesRegex(SoakSamplingError, "bounded execution time"):
            MODULE.collect_one_process(
                [sys.executable, "-c", "import time; time.sleep(5)"],
                expected_elapsed=0.0,
                timeout_seconds=0.05,
            )

        with self.assertRaisesRegex(SoakSamplingError, "synthetic failure"):
            MODULE.collect_one_process(
                [
                    sys.executable,
                    "-c",
                    "import sys; print('synthetic failure', file=sys.stderr); sys.exit(2)",
                ],
                expected_elapsed=0.0,
                timeout_seconds=10.0,
            )

    def test_collector_output_limits_are_enforced_while_child_is_running(self) -> None:
        for fd, label, limit in (
            (1, "stdout", MODULE.MAX_COLLECTOR_STDOUT_BYTES),
            (2, "stderr", MODULE.MAX_COLLECTOR_STDERR_BYTES),
        ):
            with self.subTest(label=label):
                code = (
                    "import sys,time;"
                    f"stream=sys.stdout if {fd} == 1 else sys.stderr;"
                    f"stream.write('x' * {limit + 1});stream.flush();"
                    "time.sleep(5)"
                )
                with self.assertRaisesRegex(
                    SoakSamplingError,
                    rf"collector {label} exceeds bounded size",
                ):
                    MODULE.collect_one_process(
                        [sys.executable, "-c", code],
                        expected_elapsed=0.0,
                        timeout_seconds=10.0,
                    )

    def test_collect_one_process_accepts_valid_bounded_utf8_json(self) -> None:
        expected = sample(0.0)
        code = f"print({json.dumps(json.dumps(expected))})"
        self.assertEqual(
            MODULE.collect_one_process(
                [sys.executable, "-c", code],
                expected_elapsed=0.0,
                timeout_seconds=10.0,
            ),
            expected,
        )

    def test_run_sampling_records_actual_elapsed_and_durable_jsonl(self) -> None:
        clock = FakeClock()
        collected: list[float] = []

        def collect(elapsed: float) -> dict[str, object]:
            collected.append(elapsed)
            return sample(elapsed)

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
            return sample(elapsed)

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

        self.assertEqual(samples, [0.0, 7.5, 12.5])

    def test_slow_collection_skips_missed_intervals_without_catch_up_duplicates(self) -> None:
        clock = FakeClock()
        samples: list[float] = []

        def collect(elapsed: float) -> dict[str, object]:
            samples.append(elapsed)
            clock.value += 17.0
            return sample(elapsed)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "samples.jsonl")
            with MODULE.open_output_exclusive(path) as output:
                MODULE.run_sampling(
                    duration_seconds=30.0,
                    interval_seconds=5.0,
                    collect=collect,
                    output=output,
                    monotonic=clock.monotonic,
                    sleeper=clock.sleep,
                )

        self.assertEqual(samples, [0.0, 20.0, 37.0])
        self.assertEqual(samples, sorted(set(samples)))
        self.assertGreaterEqual(samples[-1], 30.0)

    def test_existing_output_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "samples.jsonl")
            path.write_text("original\n", encoding="utf-8")
            with self.assertRaisesRegex(SoakSamplingError, "refusing to overwrite"):
                MODULE.open_output_exclusive(path)
            self.assertEqual(path.read_text(encoding="utf-8"), "original\n")

    def test_backward_monotonic_clock_fails_closed(self) -> None:
        values = iter((99.0,))
        with self.assertRaisesRegex(SoakSamplingError, "moved backwards"):
            MODULE.wait_until(
                101.0,
                started_at=100.0,
                monotonic=lambda: next(values),
                sleeper=lambda _: None,
            )


if __name__ == "__main__":
    unittest.main()
