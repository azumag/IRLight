from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "assemble_soak_report", ROOT / "scripts" / "assemble-soak-report.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

SoakAssemblyError = MODULE.SoakAssemblyError
assemble_report = MODULE.assemble_report
load_samples_jsonl = MODULE.load_samples_jsonl


def sample(
    elapsed: float,
    *,
    timestamp_errors: int = 0,
    reconnects: int = 0,
) -> dict[str, object]:
    return {
        "elapsed_seconds": elapsed,
        "memory_rss_bytes": 100,
        "cpu_percent": 12.5,
        "open_fds": 10,
        "processes": 4,
        "zombies": 0,
        "bitrate_bps": 4_000_000,
        "av_sync_drift_ms": 5.0,
        "timestamp_errors": timestamp_errors,
        "unexpected_reconnects": reconnects,
    }


class AssembleSoakReportTest(unittest.TestCase):
    def test_valid_pass_report_is_accepted_by_canonical_validator(self) -> None:
        value = assemble_report(
            samples=[sample(0), sample(60)],
            run_id=str(uuid.uuid4()),
            scenario="RTMP 1080p30 baseline",
            target_duration_seconds=60,
            outcome="pass",
            cleanup_verified=True,
            cleanup_details="disposable compose project removed",
            notes="local mock destination",
        )
        self.assertEqual(value["schema_version"], 1)
        self.assertEqual(len(value["samples"]), 2)

    def test_canonical_validator_rejects_bad_order_and_incomplete_pass(self) -> None:
        with self.assertRaisesRegex(SoakAssemblyError, "invalid"):
            assemble_report(
                samples=[sample(10), sample(5)],
                run_id=str(uuid.uuid4()),
                scenario="bad order",
                target_duration_seconds=60,
                outcome="fail",
                cleanup_verified=False,
                cleanup_details="not removed",
                notes="",
            )

        with self.assertRaisesRegex(SoakAssemblyError, "invalid"):
            assemble_report(
                samples=[sample(0), sample(59)],
                run_id=str(uuid.uuid4()),
                scenario="short pass",
                target_duration_seconds=60,
                outcome="pass",
                cleanup_verified=True,
                cleanup_details="removed",
                notes="",
            )

    def test_loader_preserves_order_and_rejects_blank_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "samples.jsonl"
            path.write_text(
                json.dumps(sample(0)) + "\n" + json.dumps(sample(60)) + "\n",
                encoding="utf-8",
            )
            values = load_samples_jsonl(path)
            self.assertEqual([item["elapsed_seconds"] for item in values], [0, 60])

            path.write_text(
                json.dumps(sample(0)) + "\n\n" + json.dumps(sample(60)),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(SoakAssemblyError, "must not be blank"):
                load_samples_jsonl(path)

    def test_loader_rejects_duplicate_keys_constants_non_objects_and_invalid_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "samples.jsonl"

            path.write_text(
                '{"elapsed_seconds":0,"elapsed_seconds":1}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(SoakAssemblyError, "duplicate JSON key"):
                load_samples_jsonl(path)

            path.write_text('{"elapsed_seconds":NaN}\n', encoding="utf-8")
            with self.assertRaisesRegex(SoakAssemblyError, "non-standard JSON"):
                load_samples_jsonl(path)

            path.write_text("[]\n", encoding="utf-8")
            with self.assertRaisesRegex(SoakAssemblyError, "JSON object"):
                load_samples_jsonl(path)

            path.write_bytes(b"{\xff}\n")
            with self.assertRaisesRegex(SoakAssemblyError, "cannot read samples"):
                load_samples_jsonl(path)

    def test_loader_rejects_symlink_fifo_and_oversized_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            target = directory / "target.jsonl"
            target.write_text(json.dumps(sample(0)) + "\n", encoding="utf-8")

            symlink = directory / "samples-symlink.jsonl"
            symlink.symlink_to(target)
            with self.assertRaisesRegex(SoakAssemblyError, "regular file"):
                load_samples_jsonl(symlink)

            if hasattr(os, "mkfifo"):
                fifo = directory / "samples.fifo"
                os.mkfifo(fifo)
                with self.assertRaisesRegex(SoakAssemblyError, "regular file"):
                    load_samples_jsonl(fifo)

            oversized = directory / "oversized.jsonl"
            with oversized.open("wb") as handle:
                handle.truncate(MODULE.MAX_SAMPLES_JSONL_BYTES + 1)
            with self.assertRaisesRegex(SoakAssemblyError, "byte limit"):
                load_samples_jsonl(oversized)

    def test_loader_rejects_path_replacement_while_opening(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            path = directory / "samples.jsonl"
            replacement = directory / "replacement.jsonl"
            backup = directory / "original.jsonl"
            path.write_text(json.dumps(sample(0)) + "\n", encoding="utf-8")
            replacement.write_text(json.dumps(sample(60)) + "\n", encoding="utf-8")

            real_open = MODULE.os.open
            swapped = False

            def swapping_open(target: object, flags: int, mode: int = 0o777) -> int:
                nonlocal swapped
                if not swapped and Path(target) == path:
                    path.replace(backup)
                    replacement.replace(path)
                    swapped = True
                return real_open(target, flags, mode)

            with mock.patch.object(MODULE.os, "open", side_effect=swapping_open):
                with self.assertRaisesRegex(SoakAssemblyError, "changed while opening"):
                    load_samples_jsonl(path)

    def test_loader_rejects_in_place_mutation_while_reading(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "samples.jsonl"
            path.write_text(
                json.dumps(sample(0)) + "\n" + json.dumps(sample(60)) + "\n",
                encoding="utf-8",
            )

            real_read = MODULE.os.read
            mutated = False

            def read_then_mutate(fd: int, size: int) -> bytes:
                nonlocal mutated
                chunk = real_read(fd, size)
                if chunk and not mutated:
                    path.write_text(
                        json.dumps(sample(0)) + "\n" + json.dumps(sample(120)) + "\n",
                        encoding="utf-8",
                    )
                    mutated = True
                return chunk

            with mock.patch.object(MODULE.os, "read", side_effect=read_then_mutate):
                with self.assertRaisesRegex(SoakAssemblyError, "changed while reading"):
                    load_samples_jsonl(path)

    def test_loader_normalizes_recursive_json_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "samples.jsonl"
            path.write_text(json.dumps(sample(0)) + "\n", encoding="utf-8")
            with mock.patch.object(MODULE.json, "loads", side_effect=RecursionError("deep")):
                with self.assertRaisesRegex(SoakAssemblyError, "nesting limit"):
                    load_samples_jsonl(path)

    def test_main_writes_deterministic_report_without_clobbering_raw_samples(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            samples_path = directory / "samples.jsonl"
            report_path = directory / "report.json"
            samples_path.write_text(
                json.dumps(sample(0)) + "\n" + json.dumps(sample(60)) + "\n",
                encoding="utf-8",
            )
            run_id = str(uuid.uuid4())
            argv = [
                "--samples-jsonl",
                str(samples_path),
                "--run-id",
                run_id,
                "--scenario",
                "RTMP 1080p30 baseline",
                "--target-duration-seconds",
                "60",
                "--outcome",
                "pass",
                "--cleanup-verified",
                "--cleanup-details",
                "compose project removed",
                "--notes",
                "local mock destination",
                "--output",
                str(report_path),
            ]
            self.assertEqual(MODULE.main(argv), 0)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["run_id"], run_id)
            self.assertEqual(len(report["samples"]), 2)

            self.assertEqual(MODULE.main(argv), 2)
            self.assertEqual(
                MODULE.main(
                    [
                        "--samples-jsonl",
                        str(samples_path),
                        "--run-id",
                        run_id,
                        "--scenario",
                        "diagnostic",
                        "--target-duration-seconds",
                        "60",
                        "--outcome",
                        "fail",
                        "--cleanup-details",
                        "not checked",
                        "--output",
                        str(samples_path),
                    ]
                ),
                2,
            )


if __name__ == "__main__":
    unittest.main()
