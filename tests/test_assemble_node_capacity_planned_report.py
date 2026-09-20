from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
import uuid


ROOT = pathlib.Path(__file__).resolve().parents[1]
ASSEMBLER_PATH = ROOT / "scripts" / "assemble-node-capacity-planned-report.py"
PLAN_RENDERER_PATH = ROOT / "scripts" / "render-node-capacity-load-plan.py"


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ASSEMBLER = _load("node_capacity_planned_report_test", ASSEMBLER_PATH)
PLAN_RENDERER = _load("node_capacity_plan_renderer_for_planned_report_test", PLAN_RENDERER_PATH)


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


class PlannedNodeCapacityReportTests(unittest.TestCase):
    def _fixture(
        self,
        directory: pathlib.Path,
        levels: list[int],
        *,
        profile_label: str = "720p30 3Mbps",
        completed_plan: bool | None = None,
    ) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
        plan_value = PLAN_RENDERER.build_plan(profile_label)
        plan = directory / "plan.json"
        plan.write_text(json.dumps(plan_value, ensure_ascii=False), encoding="utf-8")

        records = []
        for sessions in levels:
            outcome = "pass" if sessions <= 2 else "fail"
            records.append(_trial(sessions, outcome))
        trials = directory / "normal-input.trials.jsonl"
        trials.write_text(
            "\n".join(json.dumps(record, separators=(",", ":")) for record in records) + "\n",
            encoding="utf-8",
        )

        planned_levels = [1, 2, 4, 8]
        is_complete = levels == planned_levels if completed_plan is None else completed_plan
        manifest_value = {
            "schema_version": 1,
            "plan_sha256": ASSEMBLER._canonical_digest(plan_value),
            "profile_label": profile_label,
            "scenario_id": "normal-input",
            "failure_policy": "continue",
            "planned_load_levels": planned_levels,
            "tested_load_levels": levels,
            "boundary_found": any(record["outcome"] == "fail" for record in records),
            "completed_plan": is_complete,
            "trials_sha256": ASSEMBLER._canonical_digest(records),
        }
        manifest = directory / "normal-input.run.json"
        manifest.write_text(json.dumps(manifest_value), encoding="utf-8")
        return plan, trials, manifest

    def _assemble(
        self,
        plan: pathlib.Path,
        trials: pathlib.Path,
        manifest: pathlib.Path,
        scenario: str = "normal-input",
    ):
        return ASSEMBLER.assemble_planned_report(
            plan_path=plan,
            trials_path=trials,
            run_manifest_path=manifest,
            scenario_id=scenario,
            run_id=str(uuid.uuid4()),
            node_profile="c3.large-like",
            software_revision="a" * 40,
            safety_margin_percent=20,
            notes="fixture",
        )

    def test_complete_canonical_ladder_assembles_profile_bound_report(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            plan, trials, manifest = self._fixture(
                directory,
                [1, 2, 4, 8],
                profile_label="720p30 3Mbps",
            )

            report = self._assemble(plan, trials, manifest)

            self.assertEqual(
                report["scenario"],
                "profile=720p30 3Mbps; scenario=normal-input;",
            )
            self.assertEqual(
                [trial["concurrent_sessions"] for trial in report["trials"]],
                [1, 2, 4, 8],
            )

    def test_partial_ladder_manifest_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            plan, trials, manifest = self._fixture(directory, [1, 2, 4])

            with self.assertRaisesRegex(
                ASSEMBLER.PlannedReportError,
                "completed scenario plan",
            ):
                self._assemble(plan, trials, manifest)

    def test_extra_unplanned_load_level_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            plan, trials, manifest = self._fixture(
                directory,
                [1, 2, 4, 8, 16],
                completed_plan=True,
            )

            with self.assertRaisesRegex(
                ASSEMBLER.PlannedReportError,
                "planned load levels|measured load levels|complete canonical scenario plan",
            ):
                self._assemble(plan, trials, manifest)

    def test_unknown_scenario_is_rejected_without_reflecting_it(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            plan, trials, manifest = self._fixture(directory, [1, 2, 4, 8])

            with self.assertRaisesRegex(
                ASSEMBLER.PlannedReportError,
                "scenario is not present",
            ) as caught:
                self._assemble(plan, trials, manifest, scenario="bad\x1b[31m-scenario")
            self.assertNotIn("\x1b", str(caught.exception))

    def test_different_profile_plan_cannot_rebind_existing_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            _plan, trials, manifest = self._fixture(
                directory,
                [1, 2, 4, 8],
                profile_label="720p30 3Mbps",
            )
            other_plan = directory / "other-plan.json"
            other_plan.write_text(
                json.dumps(PLAN_RENDERER.build_plan("1080p30 6Mbps"), ensure_ascii=False),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ASSEMBLER.PlannedReportError,
                "does not match the canonical load plan",
            ):
                self._assemble(other_plan, trials, manifest)

    def test_tampered_trials_are_rejected_by_manifest_digest(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            plan, trials, manifest = self._fixture(directory, [1, 2, 4, 8])
            records = [json.loads(line) for line in trials.read_text(encoding="utf-8").splitlines()]
            records[1]["cpu_peak_percent"] = 99.0
            trials.write_text(
                "\n".join(json.dumps(record) for record in records) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ASSEMBLER.PlannedReportError,
                "does not match the raw trials",
            ):
                self._assemble(plan, trials, manifest)

    def test_cli_does_not_replace_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            plan, trials, manifest = self._fixture(directory, [1, 2, 4, 8])
            output = directory / "report.json"
            output.write_text("known-good\n", encoding="utf-8")

            result = ASSEMBLER.main(
                [
                    "--plan",
                    str(plan),
                    "--trials-jsonl",
                    str(trials),
                    "--run-manifest",
                    str(manifest),
                    "--scenario",
                    "normal-input",
                    "--run-id",
                    str(uuid.uuid4()),
                    "--node-profile",
                    "c3.large-like",
                    "--software-revision",
                    "b" * 40,
                    "--safety-margin-percent",
                    "20",
                    "--output",
                    str(output),
                ]
            )

            self.assertEqual(result, 2)
            self.assertEqual(output.read_text(encoding="utf-8"), "known-good\n")

    def test_invalid_trial_sequence_remains_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            plan, trials, manifest = self._fixture(directory, [1, 2, 4, 8])
            records = [json.loads(line) for line in trials.read_text(encoding="utf-8").splitlines()]
            records[3]["outcome"] = "pass"
            records[3]["failed_sessions"] = 0
            trials.write_text(
                "\n".join(json.dumps(record) for record in records) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ASSEMBLER.PlannedReportError, "raw trials are invalid"):
                self._assemble(plan, trials, manifest)


if __name__ == "__main__":
    unittest.main()
