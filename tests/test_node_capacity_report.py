from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "validate_node_capacity_report", ROOT / "scripts" / "validate-node-capacity-report.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
CapacityReportError = MODULE.CapacityReportError


def report() -> dict:
    return {
        "schema_version": 1,
        "run_id": str(uuid.uuid4()),
        "node_profile": "linux-x86_64 4 vCPU 8 GiB",
        "software_revision": "a" * 40,
        "scenario": "steady mixed 720p30/1080p30 pass-through",
        "safety_margin_percent": 25,
        "trials": [
            {
                "concurrent_sessions": 1,
                "duration_seconds": 120,
                "outcome": "pass",
                "cpu_peak_percent": 35.0,
                "memory_rss_peak_bytes": 500_000_000,
                "egress_peak_bps": 5_000_000,
                "failed_sessions": 0,
                "unexpected_reconnects": 0,
            },
            {
                "concurrent_sessions": 4,
                "duration_seconds": 120,
                "outcome": "pass",
                "cpu_peak_percent": 180.0,
                "memory_rss_peak_bytes": 900_000_000,
                "egress_peak_bps": 20_000_000,
                "failed_sessions": 0,
                "unexpected_reconnects": 0,
            },
            {
                "concurrent_sessions": 8,
                "duration_seconds": 120,
                "outcome": "fail",
                "cpu_peak_percent": 370.0,
                "memory_rss_peak_bytes": 1_700_000_000,
                "egress_peak_bps": 40_000_000,
                "failed_sessions": 1,
                "unexpected_reconnects": 0,
            },
        ],
        "notes": "synthetic fixture",
    }


class CapacityReportTest(unittest.TestCase):
    def test_derives_capacity_from_measured_boundary_and_explicit_margin(self) -> None:
        summary = MODULE.validate_report(report())
        self.assertEqual(summary["highest_passing_sessions"], 4)
        self.assertEqual(summary["first_failing_sessions"], 8)
        self.assertEqual(summary["recommended_max_sessions"], 3)
        self.assertEqual(summary["highest_pass_duration_seconds"], 120.0)
        self.assertEqual(summary["tested_load_levels"], [1, 4, 8])

    def test_requires_exact_schema_and_rejects_duplicate_json_keys(self) -> None:
        value = report()
        value["extra"] = True
        with self.assertRaisesRegex(CapacityReportError, "fields do not match schema"):
            MODULE.validate_report(value)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.json"
            path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
            with self.assertRaisesRegex(CapacityReportError, "duplicate JSON key"):
                MODULE.load_report(path)

    def test_load_report_rejects_symbolic_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "report-target.json"
            target.write_text("{}", encoding="utf-8")
            alias = root / "report.json"
            alias.symlink_to(target)
            with self.assertRaisesRegex(CapacityReportError, "regular file"):
                MODULE.load_report(alias)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO creation is unavailable")
    def test_load_report_rejects_fifo_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fifo = Path(tmp) / "report.fifo"
            os.mkfifo(fifo)
            with self.assertRaisesRegex(CapacityReportError, "regular file"):
                MODULE.load_report(fifo)

    def test_rejects_non_finite_metrics_and_boolean_numbers(self) -> None:
        for field, value in (
            ("cpu_peak_percent", float("nan")),
            ("egress_peak_bps", float("inf")),
            ("duration_seconds", True),
        ):
            candidate = report()
            candidate["trials"][0][field] = value
            with self.subTest(field=field, value=value):
                with self.assertRaises(CapacityReportError):
                    MODULE.validate_report(candidate)

    def test_requires_strictly_increasing_unique_load_levels(self) -> None:
        candidate = report()
        candidate["trials"][1]["concurrent_sessions"] = 1
        with self.assertRaisesRegex(CapacityReportError, "strictly increasing"):
            MODULE.validate_report(candidate)

    def test_rejects_pass_above_failure_to_prevent_cherry_picked_boundary(self) -> None:
        candidate = report()
        candidate["trials"][1]["outcome"] = "fail"
        candidate["trials"][2]["outcome"] = "pass"
        candidate["trials"][2]["failed_sessions"] = 0
        with self.assertRaisesRegex(CapacityReportError, "passing trial cannot appear above"):
            MODULE.validate_report(candidate)

    def test_requires_both_passing_and_failing_boundary(self) -> None:
        for outcome in ("pass", "fail"):
            candidate = report()
            for trial in candidate["trials"]:
                trial["outcome"] = outcome
                trial["failed_sessions"] = 0 if outcome == "pass" else 1
            with self.subTest(outcome=outcome):
                with self.assertRaises(CapacityReportError):
                    MODULE.validate_report(candidate)

    def test_pass_cannot_hide_failed_sessions_or_unexpected_reconnects(self) -> None:
        for field in ("failed_sessions", "unexpected_reconnects"):
            candidate = report()
            candidate["trials"][0][field] = 1
            with self.subTest(field=field):
                with self.assertRaisesRegex(CapacityReportError, "cannot claim pass"):
                    MODULE.validate_report(candidate)

    def test_rejects_failed_session_count_above_concurrency(self) -> None:
        candidate = report()
        candidate["trials"][-1]["failed_sessions"] = 9
        with self.assertRaisesRegex(CapacityReportError, "cannot exceed concurrent_sessions"):
            MODULE.validate_report(candidate)

    def test_margin_must_be_explicit_bounded_integer(self) -> None:
        for value in (0, 91, 100, True, 20.5):
            candidate = report()
            candidate["safety_margin_percent"] = value
            with self.subTest(value=value):
                with self.assertRaisesRegex(CapacityReportError, "safety_margin_percent"):
                    MODULE.validate_report(candidate)

        candidate = report()
        candidate["trials"][1]["concurrent_sessions"] = 100
        candidate["trials"][2]["concurrent_sessions"] = 101
        candidate["safety_margin_percent"] = 90
        summary = MODULE.validate_report(candidate)
        self.assertEqual(summary["recommended_max_sessions"], 10)

    def test_capacity_derivation_uses_integer_arithmetic_for_large_session_counts(self) -> None:
        candidate = report()
        huge = 10**400
        candidate["trials"][1]["concurrent_sessions"] = huge
        candidate["trials"][2]["concurrent_sessions"] = huge + 1
        summary = MODULE.validate_report(candidate)
        self.assertEqual(summary["recommended_max_sessions"], huge * 75 // 100)

    def test_rejects_evidence_that_rounds_safe_capacity_to_zero(self) -> None:
        candidate = report()
        candidate["trials"] = [candidate["trials"][0], candidate["trials"][-1]]
        candidate["safety_margin_percent"] = 50
        with self.assertRaisesRegex(CapacityReportError, "positive max_sessions"):
            MODULE.validate_report(candidate)

    def test_schema_version_requires_integer_not_numeric_equivalence(self) -> None:
        candidate = report()
        candidate["schema_version"] = 1.0
        with self.assertRaisesRegex(CapacityReportError, "schema_version"):
            MODULE.validate_report(candidate)

    def test_revision_and_uuid_are_fixed_format(self) -> None:
        for key, value in (("run_id", "not-a-uuid"), ("software_revision", "ABC")):
            candidate = report()
            candidate[key] = value
            with self.subTest(key=key):
                with self.assertRaises(CapacityReportError):
                    MODULE.validate_report(candidate)


if __name__ == "__main__":
    unittest.main()
