from __future__ import annotations

import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr


ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "scripts" / "run-node-capacity-scenario.py"
PLAN_RENDERER_PATH = ROOT / "scripts" / "render-node-capacity-load-plan.py"


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


RUNNER = _load("node_capacity_scenario_runner_test", RUNNER_PATH)
PLAN_RENDERER = _load("node_capacity_load_plan_renderer_for_runner_test", PLAN_RENDERER_PATH)


HARNESS_SOURCE = textwrap.dedent(
    """
    import json
    import os
    import pathlib
    import sys
    import time

    mode = sys.argv[1]
    request_path = pathlib.Path(sys.argv[-2])
    result_path = pathlib.Path(sys.argv[-1])
    request = json.loads(request_path.read_text(encoding="utf-8"))

    if mode == "nonzero":
        raise SystemExit(7)
    if mode == "timeout":
        time.sleep(5)
        raise SystemExit(0)
    if mode == "symlink":
        os.symlink(request_path, result_path)
        raise SystemExit(0)

    sessions = request["concurrent_sessions"]
    if mode == "pass-then-fail":
        outcome = "fail" if sessions >= 4 else "pass"
    elif mode == "all-pass":
        outcome = "pass"
    else:
        outcome = "pass"

    result = {
        "duration_seconds": 1.25,
        "outcome": outcome,
        "cpu_peak_percent": 42.5,
        "memory_rss_peak_bytes": 1048576,
        "egress_peak_bps": 3000000.0,
        "failed_sessions": 1 if outcome == "fail" else 0,
        "unexpected_reconnects": 0,
    }
    if mode == "extra-field":
        result["unexpected"] = True
    result_path.write_text(json.dumps(result), encoding="utf-8")
    """
)


class NodeCapacityScenarioRunnerTests(unittest.TestCase):
    def _write_plan(self, directory: pathlib.Path, profile: str = "720p30 3Mbps") -> pathlib.Path:
        path = directory / "plan.json"
        path.write_text(
            json.dumps(PLAN_RENDERER.build_plan(profile), ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    def _write_harness(self, directory: pathlib.Path) -> pathlib.Path:
        path = directory / "harness.py"
        path.write_text(HARNESS_SOURCE, encoding="utf-8")
        return path

    def _run(
        self,
        directory: pathlib.Path,
        mode: str,
        *,
        profile: str = "720p30 3Mbps",
        timeout_seconds: float = 2.0,
    ) -> tuple[dict[str, object], pathlib.Path]:
        plan = self._write_plan(directory, profile)
        harness = self._write_harness(directory)
        trials = directory / "trials.jsonl"
        summary = RUNNER.run_scenario(
            plan_path=plan,
            scenario_id="normal-input",
            trials_jsonl=trials,
            runner_command=[sys.executable, str(harness), mode],
            timeout_seconds=timeout_seconds,
        )
        return summary, trials

    def test_runs_levels_in_order_and_stops_after_first_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            summary, trials = self._run(directory, "pass-then-fail")

            self.assertEqual(summary["tested_load_levels"], [1, 2, 4])
            self.assertTrue(summary["boundary_found"])
            self.assertTrue(summary["stopped_after_first_failure"])
            records = [json.loads(line) for line in trials.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([record["concurrent_sessions"] for record in records], [1, 2, 4])
            self.assertEqual([record["outcome"] for record in records], ["pass", "pass", "fail"])

    def test_all_pass_records_plan_without_inventing_a_higher_level(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            summary, trials = self._run(directory, "all-pass")

            self.assertEqual(summary["tested_load_levels"], [1, 2, 4, 8])
            self.assertFalse(summary["boundary_found"])
            records = [json.loads(line) for line in trials.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([record["concurrent_sessions"] for record in records], [1, 2, 4, 8])

    def test_unknown_scenario_fails_before_harness_execution(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            plan = self._write_plan(directory)
            harness = self._write_harness(directory)
            trials = directory / "trials.jsonl"
            with self.assertRaises(RUNNER.ScenarioRunError):
                RUNNER.run_scenario(
                    plan_path=plan,
                    scenario_id="not-a-scenario",
                    trials_jsonl=trials,
                    runner_command=[sys.executable, str(harness), "all-pass"],
                    timeout_seconds=2.0,
                )
            self.assertFalse(trials.exists())

    def test_existing_evidence_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            plan = self._write_plan(directory)
            harness = self._write_harness(directory)
            trials = directory / "trials.jsonl"
            trials.write_text("known-good\n", encoding="utf-8")
            with self.assertRaises(RUNNER.ScenarioRunError):
                RUNNER.run_scenario(
                    plan_path=plan,
                    scenario_id="normal-input",
                    trials_jsonl=trials,
                    runner_command=[sys.executable, str(harness), "all-pass"],
                    timeout_seconds=2.0,
                )
            self.assertEqual(trials.read_text(encoding="utf-8"), "known-good\n")

    def test_timeout_and_nonzero_exit_do_not_create_a_trial(self) -> None:
        for mode, timeout in (("timeout", 0.05), ("nonzero", 2.0)):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as raw_directory:
                directory = pathlib.Path(raw_directory)
                with self.assertRaises(RUNNER.ScenarioRunError):
                    self._run(directory, mode, timeout_seconds=timeout)
                self.assertFalse((directory / "trials.jsonl").exists())

    def test_result_schema_and_symlink_are_fail_closed(self) -> None:
        for mode in ("extra-field", "symlink"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as raw_directory:
                directory = pathlib.Path(raw_directory)
                with self.assertRaises(RUNNER.ScenarioRunError):
                    self._run(directory, mode)
                self.assertFalse((directory / "trials.jsonl").exists())

    def test_failure_output_does_not_reflect_profile_label(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            plan = self._write_plan(directory, "720p30\x1b[31m")
            harness = self._write_harness(directory)
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                result = RUNNER.main(
                    [
                        "--plan",
                        str(plan),
                        "--scenario",
                        "normal-input",
                        "--trials-jsonl",
                        str(directory / "trials.jsonl"),
                        "--timeout-seconds",
                        "2",
                        "--runner",
                        sys.executable,
                        "--runner-arg",
                        str(harness),
                        "--runner-arg",
                        "nonzero",
                    ]
                )
            self.assertEqual(result, 2)
            self.assertNotIn("\x1b", stderr.getvalue())
            self.assertNotIn("720p30", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
