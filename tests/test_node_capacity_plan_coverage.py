from __future__ import annotations

import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import unittest
import uuid
from contextlib import redirect_stdout


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validate-node-capacity-plan-coverage.py"
SPEC = importlib.util.spec_from_file_location("node_capacity_plan_coverage", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

RENDERER_PATH = ROOT / "scripts" / "render-node-capacity-load-plan.py"
RENDERER_SPEC = importlib.util.spec_from_file_location(
    "node_capacity_plan_coverage_test_renderer", RENDERER_PATH
)
assert RENDERER_SPEC is not None and RENDERER_SPEC.loader is not None
RENDERER = importlib.util.module_from_spec(RENDERER_SPEC)
sys.modules[RENDERER_SPEC.name] = RENDERER
RENDERER_SPEC.loader.exec_module(RENDERER)

PROFILE = "720p30/1080p30 mix qa-v1"
REVISION = "0123456789abcdef0123456789abcdef01234567"
SCENARIO_IDS = [scenario_id for scenario_id, _description in RENDERER.SCENARIOS]


def make_report(
    scenario_id: str,
    run_number: int,
    *,
    levels: tuple[int, ...] = (1, 2, 4, 8),
    revision: str = REVISION,
    node_profile: str = "linux-x86_64 4 vCPU 8 GiB",
    margin: int = 25,
) -> dict[str, object]:
    trials = []
    for index, sessions in enumerate(levels):
        outcome = "fail" if index == len(levels) - 1 else "pass"
        trials.append(
            {
                "concurrent_sessions": sessions,
                "duration_seconds": 300.0,
                "outcome": outcome,
                "cpu_peak_percent": float(sessions * 20),
                "memory_rss_peak_bytes": sessions * 100_000_000,
                "egress_peak_bps": float(sessions * 5_000_000),
                "failed_sessions": 1 if outcome == "fail" else 0,
                "unexpected_reconnects": 0,
            }
        )
    return {
        "schema_version": 1,
        "run_id": str(uuid.UUID(int=run_number)),
        "node_profile": node_profile,
        "software_revision": revision,
        "scenario": f"{PROFILE}; scenario={scenario_id}; approved test policy",
        "safety_margin_percent": margin,
        "trials": trials,
        "notes": "test fixture",
    }


class NodeCapacityPlanCoverageTests(unittest.TestCase):
    def write_fixture_set(
        self,
        directory: pathlib.Path,
        *,
        report_overrides: dict[str, dict[str, object]] | None = None,
    ) -> tuple[pathlib.Path, dict[str, pathlib.Path]]:
        plan_path = directory / "plan.json"
        plan_path.write_text(
            json.dumps(RENDERER.build_plan(PROFILE), sort_keys=True),
            encoding="utf-8",
        )
        report_paths: dict[str, pathlib.Path] = {}
        overrides = report_overrides or {}
        for index, scenario_id in enumerate(SCENARIO_IDS, start=1):
            report = make_report(scenario_id, index)
            report.update(overrides.get(scenario_id, {}))
            report_path = directory / f"{scenario_id}.json"
            report_path.write_text(json.dumps(report, sort_keys=True), encoding="utf-8")
            report_paths[scenario_id] = report_path
        return plan_path, report_paths

    def test_canonical_reports_cover_every_planned_scenario(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            plan_path, reports = self.write_fixture_set(pathlib.Path(tempdir))
            summary = MODULE.validate_coverage(plan_path, reports)

        self.assertTrue(summary["valid"])
        self.assertEqual(summary["planned_session_counts"], [1, 2, 4, 8])
        self.assertEqual(
            [result["scenario_id"] for result in summary["scenario_results"]],
            SCENARIO_IDS,
        )
        self.assertEqual(summary["recommended_max_sessions"], 3)
        self.assertEqual(summary["software_revision"], REVISION)

    def test_missing_or_unknown_scenario_binding_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            plan_path, reports = self.write_fixture_set(pathlib.Path(tempdir))
            reports.pop(SCENARIO_IDS[-1])
            reports["not-in-plan"] = pathlib.Path(tempdir) / "unused.json"
            with self.assertRaisesRegex(
                MODULE.CapacityCoverageError, "report bindings do not match plan scenarios"
            ):
                MODULE.validate_coverage(plan_path, reports)

    def test_missing_planned_load_level_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = pathlib.Path(tempdir)
            plan_path, reports = self.write_fixture_set(root)
            report = make_report(SCENARIO_IDS[0], 1, levels=(1, 2, 8))
            reports[SCENARIO_IDS[0]].write_text(
                json.dumps(report, sort_keys=True), encoding="utf-8"
            )
            with self.assertRaisesRegex(
                MODULE.CapacityCoverageError, "missing planned load levels: 4"
            ):
                MODULE.validate_coverage(plan_path, reports)

    def test_report_must_identify_plan_profile_label(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = pathlib.Path(tempdir)
            plan_path, reports = self.write_fixture_set(root)
            report = make_report(SCENARIO_IDS[0], 1)
            report["scenario"] = "different media profile; approved policy"
            reports[SCENARIO_IDS[0]].write_text(
                json.dumps(report, sort_keys=True), encoding="utf-8"
            )
            with self.assertRaisesRegex(
                MODULE.CapacityCoverageError, "does not identify the plan profile label"
            ):
                MODULE.validate_coverage(plan_path, reports)

    def test_reports_must_use_one_node_revision_and_margin(self) -> None:
        cases = (
            ({"node_profile": "different node"}, "different node profiles"),
            ({"software_revision": "fedcba9876543210fedcba9876543210fedcba98"}, "different software revisions"),
            ({"safety_margin_percent": 20}, "different safety margins"),
        )
        for override, expected_error in cases:
            with self.subTest(expected_error=expected_error):
                with tempfile.TemporaryDirectory() as tempdir:
                    root = pathlib.Path(tempdir)
                    plan_path, reports = self.write_fixture_set(root)
                    report = make_report(SCENARIO_IDS[1], 2)
                    report.update(override)
                    reports[SCENARIO_IDS[1]].write_text(
                        json.dumps(report, sort_keys=True), encoding="utf-8"
                    )
                    with self.assertRaisesRegex(
                        MODULE.CapacityCoverageError, expected_error
                    ):
                        MODULE.validate_coverage(plan_path, reports)

    def test_same_measured_run_cannot_satisfy_multiple_scenarios(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = pathlib.Path(tempdir)
            plan_path, reports = self.write_fixture_set(root)
            report = make_report(SCENARIO_IDS[1], 1)
            reports[SCENARIO_IDS[1]].write_text(
                json.dumps(report, sort_keys=True), encoding="utf-8"
            )
            with self.assertRaisesRegex(
                MODULE.CapacityCoverageError, "distinct measured run_id"
            ):
                MODULE.validate_coverage(plan_path, reports)

    def test_additional_measured_levels_are_allowed_after_planned_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = pathlib.Path(tempdir)
            plan_path, reports = self.write_fixture_set(root)
            for index, scenario_id in enumerate(SCENARIO_IDS, start=1):
                report = make_report(scenario_id, index, levels=(1, 2, 4, 8, 16))
                reports[scenario_id].write_text(
                    json.dumps(report, sort_keys=True), encoding="utf-8"
                )
            summary = MODULE.validate_coverage(plan_path, reports)
        self.assertEqual(summary["recommended_max_sessions"], 6)

    def test_cli_json_summary_and_duplicate_binding_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            plan_path, reports = self.write_fixture_set(pathlib.Path(tempdir))
            argv = [str(plan_path)]
            for scenario_id in SCENARIO_IDS:
                argv.extend(["--report", f"{scenario_id}={reports[scenario_id]}"])
            argv.append("--json")
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(MODULE.main(argv), 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["recommended_max_sessions"], 3)

        with self.assertRaisesRegex(
            MODULE.CapacityCoverageError, "duplicate report binding"
        ):
            MODULE.normalize_report_bindings(
                [("normal-input", pathlib.Path("a")), ("normal-input", pathlib.Path("b"))]
            )

    def test_script_has_no_load_execution_or_network_imports(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        for forbidden in (
            "import subprocess",
            "from subprocess",
            "import socket",
            "from socket",
            "import requests",
            "import urllib",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
