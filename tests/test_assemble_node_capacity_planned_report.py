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
    def _fixture(self, directory: pathlib.Path, levels: list[int]) -> tuple[pathlib.Path, pathlib.Path]:
        plan = directory / "plan.json"
        plan.write_text(
            json.dumps(PLAN_RENDERER.build_plan("720p30 3Mbps"), ensure_ascii=False),
            encoding="utf-8",
        )
        trials = directory / "normal-input.trials.jsonl"
        lines = []
        for sessions in levels:
            outcome = "pass" if sessions <= 2 else "fail"
            lines.append(json.dumps(_trial(sessions, outcome), separators=(",", ":")))
        trials.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return plan, trials

    def _assemble(self, plan: pathlib.Path, trials: pathlib.Path, scenario: str = "normal-input"):
        return ASSEMBLER.assemble_planned_report(
            plan_path=plan,
            trials_path=trials,
            scenario_id=scenario,
            run_id=str(uuid.uuid4()),
            node_profile="c3.large-like",
            software_revision="a" * 40,
            safety_margin_percent=20,
            notes="fixture",
        )

    def test_complete_canonical_ladder_assembles_report(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            plan, trials = self._fixture(directory, [1, 2, 4, 8])

            report = self._assemble(plan, trials)

            self.assertEqual(report["scenario"], "normal-input")
            self.assertEqual(
                [trial["concurrent_sessions"] for trial in report["trials"]],
                [1, 2, 4, 8],
            )

    def test_partial_ladder_is_rejected_before_report_creation(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            plan, trials = self._fixture(directory, [1, 2, 4])

            with self.assertRaisesRegex(
                ASSEMBLER.PlannedReportError,
                "complete canonical scenario plan",
            ):
                self._assemble(plan, trials)

    def test_extra_unplanned_load_level_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            plan, trials = self._fixture(directory, [1, 2, 4, 8, 16])

            with self.assertRaisesRegex(
                ASSEMBLER.PlannedReportError,
                "complete canonical scenario plan",
            ):
                self._assemble(plan, trials)

    def test_unknown_scenario_is_rejected_without_reflecting_it(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            plan, trials = self._fixture(directory, [1, 2, 4, 8])

            with self.assertRaisesRegex(
                ASSEMBLER.PlannedReportError,
                "scenario is not present",
            ) as caught:
                self._assemble(plan, trials, scenario="bad\x1b[31m-scenario")
            self.assertNotIn("\x1b", str(caught.exception))

    def test_cli_does_not_replace_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            plan, trials = self._fixture(directory, [1, 2, 4, 8])
            output = directory / "report.json"
            output.write_text("known-good\n", encoding="utf-8")

            result = ASSEMBLER.main(
                [
                    "--plan",
                    str(plan),
                    "--trials-jsonl",
                    str(trials),
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
            plan, trials = self._fixture(directory, [1, 2, 4, 8])
            records = [json.loads(line) for line in trials.read_text(encoding="utf-8").splitlines()]
            records[3]["outcome"] = "pass"
            records[3]["failed_sessions"] = 0
            trials.write_text(
                "\n".join(json.dumps(record) for record in records) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ASSEMBLER.PlannedReportError, "raw trials are invalid"):
                self._assemble(plan, trials)


if __name__ == "__main__":
    unittest.main()
