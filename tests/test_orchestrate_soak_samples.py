from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "orchestrate_soak_samples", ROOT / "scripts" / "orchestrate-soak-samples.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

SoakOrchestrationError = MODULE.SoakOrchestrationError


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class SoakSampleOrchestratorTest(unittest.TestCase):
    def test_schedule_collects_baseline_intervals_and_target(self) -> None:
        clock = FakeClock()
        observed: list[float] = []
        count = MODULE.collect_on_schedule(
            duration_seconds=10,
            interval_seconds=4,
            emit=observed.append,
            clock=clock,
            sleeper=clock.sleep,
        )
        self.assertEqual(count, 4)
        self.assertEqual(observed, [0.0, 4.0, 8.0, 10.0])

    def test_schedule_does_not_fabricate_missed_interval_timestamps(self) -> None:
        clock = FakeClock()
        observed: list[float] = []

        def emit(elapsed: float) -> None:
            observed.append(elapsed)
            if elapsed == 4.0:
                clock.now += 5.0

        MODULE.collect_on_schedule(
            duration_seconds=12,
            interval_seconds=4,
            emit=emit,
            clock=clock,
            sleeper=clock.sleep,
        )
        self.assertEqual(observed, [0.0, 4.0, 9.0, 12.0])

    def test_collector_command_is_fixed_and_parses_one_object(self) -> None:
        calls: list[list[str]] = []

        def runner(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps({"elapsed_seconds": 30, "memory_rss_bytes": 1}),
                stderr="",
            )

        sample = MODULE.run_collector(
            collector=Path("/repo/scripts/collect-soak-resource-sample.py"),
            project="irlight-poc-soak-a",
            compose_file=Path("/repo/docker-compose.poc.yml"),
            elapsed_seconds=30,
            media_metrics_file=Path("/tmp/media.json"),
            allow_unmeasured_media=False,
            runner=runner,
        )
        self.assertEqual(sample["elapsed_seconds"], 30)
        self.assertEqual(calls[0][1], "/repo/scripts/collect-soak-resource-sample.py")
        self.assertIn("--media-metrics-file", calls[0])
        self.assertNotIn("--allow-unmeasured-media", calls[0])

    def test_collector_failure_and_malformed_json_fail_closed(self) -> None:
        def failed(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 2, stdout="", stderr="bad sample")

        with self.assertRaisesRegex(SoakOrchestrationError, "collector exited 2"):
            MODULE.run_collector(
                collector=Path("collector.py"),
                project="irlight-poc-soak-a",
                compose_file=Path("compose.yml"),
                elapsed_seconds=0,
                media_metrics_file=None,
                allow_unmeasured_media=True,
                runner=failed,
            )

        for raw in ('{"a":1,"a":2}', '{"a":NaN}', "[]", ""):
            with self.subTest(raw=raw):
                with self.assertRaises(SoakOrchestrationError):
                    MODULE.parse_collector_output(raw)

    def test_persist_sample_is_canonical_and_fsyncs(self) -> None:
        class Buffer(io.StringIO):
            def fileno(self) -> int:
                return 123

        handle = Buffer()
        with mock.patch.object(MODULE.os, "fsync") as fsync:
            MODULE.persist_sample(handle, {"z": 1, "a": 2})
        self.assertEqual(handle.getvalue(), '{"a":2,"z":1}\n')
        fsync.assert_called_once_with(123)

    def test_main_preserves_existing_evidence_and_rejects_alias_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "samples.jsonl"
            output.write_text("existing\n", encoding="utf-8")
            with mock.patch.object(MODULE.Path, "is_file", return_value=True):
                rc = MODULE.main(
                    [
                        "--project",
                        "irlight-poc-soak-a",
                        "--duration-seconds",
                        "1",
                        "--interval-seconds",
                        "1",
                        "--samples-jsonl",
                        str(output),
                        "--allow-unmeasured-media",
                    ]
                )
            self.assertEqual(rc, 2)
            self.assertEqual(output.read_text(encoding="utf-8"), "existing\n")

            compose = root / "compose.yml"
            compose.write_text("services: {}\n", encoding="utf-8")
            with mock.patch.object(MODULE.Path, "is_file", return_value=True):
                rc = MODULE.main(
                    [
                        "--project",
                        "irlight-poc-soak-a",
                        "--compose-file",
                        str(compose),
                        "--duration-seconds",
                        "1",
                        "--interval-seconds",
                        "1",
                        "--samples-jsonl",
                        str(compose),
                        "--allow-unmeasured-media",
                    ]
                )
            self.assertEqual(rc, 2)
            self.assertEqual(compose.read_text(encoding="utf-8"), "services: {}\n")


if __name__ == "__main__":
    unittest.main()
