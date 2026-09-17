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
    "validate_soak_report", ROOT / "scripts" / "validate-soak-report.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

SoakReportError = MODULE.SoakReportError
load_report = MODULE.load_report
validate_report = MODULE.validate_report


def sample(
    elapsed: float,
    *,
    rss: int = 100,
    fds: int = 10,
    processes: int = 4,
    zombies: int = 0,
    bitrate: int | None = 4_000_000,
    av_sync: float | None = 5.0,
    timestamp_errors: int = 0,
    reconnects: int = 0,
) -> dict[str, object]:
    return {
        "elapsed_seconds": elapsed,
        "memory_rss_bytes": rss,
        "cpu_percent": 12.5,
        "open_fds": fds,
        "processes": processes,
        "zombies": zombies,
        "bitrate_bps": bitrate,
        "av_sync_drift_ms": av_sync,
        "timestamp_errors": timestamp_errors,
        "unexpected_reconnects": reconnects,
    }


def report() -> dict[str, object]:
    return {
        "schema_version": 1,
        "run_id": str(uuid.uuid4()),
        "scenario": "RTMP 1080p30 baseline",
        "target_duration_seconds": 60,
        "outcome": "pass",
        "samples": [
            sample(0, rss=100, fds=10),
            sample(30, rss=120, fds=11),
            sample(60, rss=115, fds=10),
        ],
        "cleanup": {"verified": True, "details": "compose project removed"},
        "notes": "",
    }


class ValidateSoakReportTest(unittest.TestCase):
    def test_valid_pass_report_produces_deterministic_deltas(self) -> None:
        value = report()
        value["samples"] = [
            sample(0, rss=100, fds=10),
            sample(30, rss=120, fds=11),
            sample(60, rss=115, fds=10),
        ]
        summary = validate_report(value)
        self.assertEqual(summary["observed_duration_seconds"], 60.0)
        self.assertEqual(summary["sample_count"], 3)
        self.assertEqual(summary["memory_rss_delta_bytes"], 15)
        self.assertEqual(summary["memory_rss_peak_bytes"], 120)
        self.assertEqual(summary["open_fds_delta"], 0)
        self.assertEqual(summary["bitrate_min_bps"], 4_000_000.0)
        self.assertEqual(summary["av_sync_abs_peak_ms"], 5.0)
        self.assertTrue(summary["cleanup_verified"])

    def test_pass_requires_duration_two_samples_and_verified_cleanup(self) -> None:
        cases = []
        too_short = report()
        too_short["samples"] = [sample(0), sample(59)]
        cases.append(too_short)
        one_sample = report()
        one_sample["samples"] = [sample(60)]
        cases.append(one_sample)
        cleanup_missing = report()
        cleanup_missing["cleanup"] = {"verified": False, "details": "not checked"}
        cases.append(cleanup_missing)
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(SoakReportError):
                    validate_report(value)

    def test_pass_requires_zero_elapsed_and_counter_baselines(self) -> None:
        delayed_start = report()
        delayed_start["samples"] = [sample(1), sample(60)]
        with self.assertRaisesRegex(SoakReportError, "elapsed_seconds 0"):
            validate_report(delayed_start)

        timestamp_baseline = report()
        timestamp_baseline["samples"] = [
            sample(0, timestamp_errors=1),
            sample(60, timestamp_errors=1),
        ]
        with self.assertRaisesRegex(SoakReportError, "baseline timestamp_errors"):
            validate_report(timestamp_baseline)

        reconnect_baseline = report()
        reconnect_baseline["samples"] = [
            sample(0, reconnects=1),
            sample(60, reconnects=1),
        ]
        with self.assertRaisesRegex(SoakReportError, "baseline unexpected_reconnects"):
            validate_report(reconnect_baseline)

    def test_fail_or_aborted_report_may_be_short_but_still_must_be_well_formed(self) -> None:
        for outcome in ("fail", "aborted"):
            value = report()
            value["outcome"] = outcome
            value["samples"] = [sample(12)]
            value["cleanup"] = {"verified": False, "details": "runner crashed"}
            summary = validate_report(value)
            self.assertEqual(summary["outcome"], outcome)

    def test_rejects_non_increasing_elapsed_and_decreasing_counters(self) -> None:
        duplicate_time = report()
        duplicate_time["samples"] = [sample(0), sample(0)]
        with self.assertRaisesRegex(SoakReportError, "strictly increasing"):
            validate_report(duplicate_time)

        decreasing_errors = report()
        decreasing_errors["samples"] = [
            sample(0, timestamp_errors=2),
            sample(60, timestamp_errors=1),
        ]
        with self.assertRaisesRegex(SoakReportError, "timestamp_errors"):
            validate_report(decreasing_errors)

        decreasing_reconnects = report()
        decreasing_reconnects["samples"] = [
            sample(0, reconnects=2),
            sample(60, reconnects=1),
        ]
        with self.assertRaisesRegex(SoakReportError, "unexpected_reconnects"):
            validate_report(decreasing_reconnects)

    def test_rejects_boolean_negative_nonfinite_and_overflowing_numbers(self) -> None:
        mutations = (
            ("schema_version", 1.0),
            ("target_duration_seconds", True),
            ("target_duration_seconds", 0),
        )
        for field, invalid in mutations:
            value = report()
            value[field] = invalid
            with self.subTest(field=field, invalid=invalid):
                with self.assertRaises(SoakReportError):
                    validate_report(value)

        invalid_samples = (
            {"memory_rss_bytes": True},
            {"open_fds": -1},
            {"cpu_percent": float("nan")},
            {"bitrate_bps": float("inf")},
            {"av_sync_drift_ms": 10**1000},
        )
        for mutation in invalid_samples:
            value = report()
            value["samples"] = [sample(0), sample(60)]
            value["samples"][1].update(mutation)  # type: ignore[index,union-attr]
            with self.subTest(mutation=mutation):
                with self.assertRaises(SoakReportError):
                    validate_report(value)

    def test_rejects_unknown_or_missing_fields(self) -> None:
        value = report()
        value["unexpected"] = 1
        with self.assertRaisesRegex(SoakReportError, "fields do not match schema"):
            validate_report(value)
        value = report()
        del value["notes"]
        with self.assertRaisesRegex(SoakReportError, "fields do not match schema"):
            validate_report(value)

    def test_loader_rejects_duplicate_keys_and_nonstandard_constants(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.json"
            path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
            with self.assertRaisesRegex(SoakReportError, "duplicate JSON key"):
                load_report(path)
            path.write_text('{"schema_version":NaN}', encoding="utf-8")
            with self.assertRaisesRegex(SoakReportError, "non-standard JSON numeric constant"):
                load_report(path)
            path.write_bytes(b"{\xff}")
            with self.assertRaisesRegex(SoakReportError, "cannot read report"):
                load_report(path)

    def test_loader_rejects_symlink_fifo_and_oversized_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            regular = root / "regular.json"
            regular.write_text(json.dumps(report()), encoding="utf-8")

            link = root / "report-link.json"
            try:
                link.symlink_to(regular)
            except (NotImplementedError, OSError):
                pass
            else:
                with self.assertRaisesRegex(SoakReportError, "regular file"):
                    load_report(link)

            if hasattr(os, "mkfifo"):
                fifo = root / "report.fifo"
                os.mkfifo(fifo)
                with self.assertRaisesRegex(SoakReportError, "regular file"):
                    load_report(fifo)

            oversized = root / "oversized.json"
            oversized.write_bytes(b" " * (MODULE.MAX_REPORT_BYTES + 1))
            with self.assertRaisesRegex(SoakReportError, "maximum size"):
                load_report(oversized)

    def test_loader_rejects_path_replacement_between_inspection_and_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "report.json"
            replacement = root / "replacement.json"
            path.write_text(json.dumps(report()), encoding="utf-8")
            replacement.write_text('{"schema_version":1}', encoding="utf-8")
            original_open = MODULE.os.open

            def replacing_open(target: object, flags: int) -> int:
                os.replace(replacement, path)
                return original_open(target, flags)

            with mock.patch.object(MODULE.os, "open", side_effect=replacing_open):
                with self.assertRaisesRegex(SoakReportError, "changed while opening"):
                    load_report(path)

    def test_loader_accepts_regular_file_at_size_limit_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.json"
            encoded = json.dumps(report()).encode("utf-8")
            self.assertLess(len(encoded), MODULE.MAX_REPORT_BYTES)
            encoded += b" " * (MODULE.MAX_REPORT_BYTES - len(encoded))
            self.assertEqual(len(encoded), MODULE.MAX_REPORT_BYTES)
            path.write_bytes(encoded)
            loaded = load_report(path)
            self.assertEqual(loaded["schema_version"], 1)

    def test_run_id_and_strings_are_bounded(self) -> None:
        value = report()
        value["run_id"] = "not-a-uuid"
        with self.assertRaisesRegex(SoakReportError, "UUID"):
            validate_report(value)
        value = report()
        value["scenario"] = " "
        with self.assertRaisesRegex(SoakReportError, "scenario"):
            validate_report(value)
        value = report()
        value["notes"] = "x" * 4001
        with self.assertRaisesRegex(SoakReportError, "notes"):
            validate_report(value)
        value = report()
        value["cleanup"] = {"verified": True, "details": "   "}
        with self.assertRaisesRegex(SoakReportError, "cleanup.details"):
            validate_report(value)


if __name__ == "__main__":
    unittest.main()
