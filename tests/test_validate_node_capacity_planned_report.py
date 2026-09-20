from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
import uuid


ROOT = pathlib.Path(__file__).resolve().parents[1]
VALIDATOR_PATH = ROOT / "scripts" / "validate-node-capacity-planned-report.py"
ASSEMBLER_PATH = ROOT / "scripts" / "assemble-node-capacity-planned-report.py"
PLAN_RENDERER_PATH = ROOT / "scripts" / "render-node-capacity-load-plan.py"
NODE_PROFILE = "c3.large-like"
SOFTWARE_REVISION = "a" * 40
PROFILE_LABEL = "720p30 3Mbps"
SCENARIO_ID = "normal-input"


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


VALIDATOR = _load("node_capacity_planned_report_validator_test", VALIDATOR_PATH)
ASSEMBLER = _load("node_capacity_planned_report_assembler_for_validator_test", ASSEMBLER_PATH)
PLAN_RENDERER = _load("node_capacity_plan_renderer_for_validator_test", PLAN_RENDERER_PATH)


def _trial(sessions: int, outcome: str) -> dict[str, object]:
    return {
        "concurrent_sessions": sessions,
        "duration_seconds": 600.0,
        "outcome": outcome,
        "cpu_peak_percent": 50.0 + sessions,
        "memory_rss_peak_bytes": 1024 * 1024 * sessions,
        "egress_peak_bps": 3_000_000.0 * sessions,
        "failed_sessions": 0 if outcome == "pass" else 1,
        "unexpected_reconnects": 0,
    }


class PersistedPlannedNodeCapacityReportTests(unittest.TestCase):
    def _fixture(
        self, directory: pathlib.Path
    ) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path, pathlib.Path]:
        plan_value = PLAN_RENDERER.build_plan(PROFILE_LABEL)
        plan = directory / "plan.json"
        plan.write_text(json.dumps(plan_value, ensure_ascii=False), encoding="utf-8")

        levels = [1, 2, 4, 8]
        records = [
            _trial(sessions, "pass" if sessions <= 2 else "fail")
            for sessions in levels
        ]
        trials = directory / "normal-input.trials.jsonl"
        trials.write_text(
            "\n".join(json.dumps(record, separators=(",", ":")) for record in records)
            + "\n",
            encoding="utf-8",
        )

        manifest_value = {
            "schema_version": 1,
            "plan_sha256": ASSEMBLER._canonical_digest(plan_value),
            "profile_label": PROFILE_LABEL,
            "scenario_id": SCENARIO_ID,
            "node_profile": NODE_PROFILE,
            "software_revision": SOFTWARE_REVISION,
            "failure_policy": "continue",
            "planned_load_levels": levels,
            "tested_load_levels": levels,
            "boundary_found": True,
            "completed_plan": True,
            "trials_sha256": ASSEMBLER._canonical_digest(records),
        }
        manifest = directory / "normal-input.run.json"
        manifest.write_text(json.dumps(manifest_value), encoding="utf-8")

        report_value = ASSEMBLER.assemble_planned_report(
            plan_path=plan,
            trials_path=trials,
            run_manifest_path=manifest,
            scenario_id=SCENARIO_ID,
            run_id=str(uuid.UUID(int=1)),
            safety_margin_percent=20,
            notes="approved fixture policy",
        )
        report = directory / "normal-input.report.json"
        report.write_text(
            json.dumps(report_value, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        return plan, trials, manifest, report

    def _validate(
        self,
        plan: pathlib.Path,
        trials: pathlib.Path,
        manifest: pathlib.Path,
        report: pathlib.Path,
        *,
        scenario: str = SCENARIO_ID,
    ):
        return VALIDATOR.validate_planned_report(
            report_path=report,
            plan_path=plan,
            trials_path=trials,
            run_manifest_path=manifest,
            scenario_id=scenario,
        )

    def test_persisted_report_matches_canonical_run_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            plan, trials, manifest, report = self._fixture(pathlib.Path(raw_directory))

            summary = self._validate(plan, trials, manifest, report)

        self.assertTrue(summary["provenance_bound"])
        self.assertEqual(summary["node_profile"], NODE_PROFILE)
        self.assertEqual(summary["software_revision"], SOFTWARE_REVISION)
        self.assertEqual(summary["tested_load_levels"], [1, 2, 4, 8])
        self.assertEqual(summary["recommended_max_sessions"], 1)

    def test_report_trial_tamper_is_rejected_even_when_report_remains_schema_valid(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            plan, trials, manifest, report = self._fixture(pathlib.Path(raw_directory))
            payload = json.loads(report.read_text(encoding="utf-8"))
            payload["trials"][1]["cpu_peak_percent"] = 88.0
            report.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(
                VALIDATOR.PlannedReportValidationError,
                "does not match canonical run provenance",
            ):
                self._validate(plan, trials, manifest, report)

    def test_raw_trial_tamper_is_rejected_by_run_manifest_digest(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            plan, trials, manifest, report = self._fixture(pathlib.Path(raw_directory))
            records = [
                json.loads(line)
                for line in trials.read_text(encoding="utf-8").splitlines()
            ]
            records[1]["cpu_peak_percent"] = 77.0
            trials.write_text(
                "\n".join(json.dumps(record) for record in records) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                VALIDATOR.PlannedReportValidationError,
                "run provenance is invalid",
            ):
                self._validate(plan, trials, manifest, report)

    def test_run_manifest_identity_tamper_cannot_silently_relabel_report(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            plan, trials, manifest, report = self._fixture(pathlib.Path(raw_directory))
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["node_profile"] = "different-node-shape"
            manifest.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(
                VALIDATOR.PlannedReportValidationError,
                "does not match canonical run provenance",
            ):
                self._validate(plan, trials, manifest, report)

    def test_different_profile_plan_cannot_rebind_persisted_report(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            _plan, trials, manifest, report = self._fixture(directory)
            other_plan = directory / "other-plan.json"
            other_plan.write_text(
                json.dumps(PLAN_RENDERER.build_plan("1080p30 6Mbps")),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                VALIDATOR.PlannedReportValidationError,
                "run provenance is invalid",
            ):
                self._validate(other_plan, trials, manifest, report)

    def test_invalid_scenario_is_fail_closed_without_reflection(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            plan, trials, manifest, report = self._fixture(pathlib.Path(raw_directory))
            malicious = "bad\x1b[31m-scenario"
            with self.assertRaises(VALIDATOR.PlannedReportValidationError) as caught:
                self._validate(
                    plan,
                    trials,
                    manifest,
                    report,
                    scenario=malicious,
                )

        self.assertNotIn("\x1b", str(caught.exception))
        self.assertNotIn(malicious, str(caught.exception))

    def test_validator_has_no_load_execution_or_network_imports(self) -> None:
        source = VALIDATOR_PATH.read_text(encoding="utf-8")
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
