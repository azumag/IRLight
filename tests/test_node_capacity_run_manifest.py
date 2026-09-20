from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import textwrap
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "scripts" / "run-node-capacity-scenario.py"
PLAN_RENDERER_PATH = ROOT / "scripts" / "render-node-capacity-load-plan.py"
NODE_PROFILE = "c3.large-like"
SOFTWARE_REVISION = "a" * 40


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


RUNNER = _load("node_capacity_runner_manifest_test", RUNNER_PATH)
PLAN_RENDERER = _load("node_capacity_runner_manifest_plan_renderer", PLAN_RENDERER_PATH)


HARNESS_SOURCE = textwrap.dedent(
    """
    import json
    import pathlib
    import sys

    mode = sys.argv[1]
    request = json.loads(pathlib.Path(sys.argv[-2]).read_text(encoding="utf-8"))
    result_path = pathlib.Path(sys.argv[-1])
    if mode == "nonzero":
        raise SystemExit(9)
    sessions = request["concurrent_sessions"]
    outcome = "fail" if sessions >= 4 else "pass"
    result_path.write_text(
        json.dumps(
            {
                "duration_seconds": 600.0,
                "outcome": outcome,
                "cpu_peak_percent": 40.0 + sessions,
                "memory_rss_peak_bytes": 1048576 * sessions,
                "egress_peak_bps": 3000000.0 * sessions,
                "failed_sessions": 1 if outcome == "fail" else 0,
                "unexpected_reconnects": 0,
            }
        ),
        encoding="utf-8",
    )
    """
)


class NodeCapacityRunManifestTests(unittest.TestCase):
    def _fixture(self, directory: pathlib.Path):
        plan_value = PLAN_RENDERER.build_plan("720p30 3Mbps")
        plan = directory / "plan.json"
        plan.write_text(json.dumps(plan_value), encoding="utf-8")
        harness = directory / "harness.py"
        harness.write_text(HARNESS_SOURCE, encoding="utf-8")
        return plan_value, plan, harness

    def _run_with_manifest(
        self,
        *,
        plan: pathlib.Path,
        harness: pathlib.Path,
        trials: pathlib.Path,
        manifest: pathlib.Path,
        failure_policy: str,
        mode: str = "normal",
    ):
        return RUNNER.run_scenario(
            plan_path=plan,
            scenario_id="normal-input",
            trials_jsonl=trials,
            runner_command=[sys.executable, str(harness), mode],
            timeout_seconds=2.0,
            failure_policy=failure_policy,
            run_manifest=manifest,
            node_profile=NODE_PROFILE,
            software_revision=SOFTWARE_REVISION,
        )

    def test_complete_run_manifest_binds_plan_trials_and_measured_identity(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            plan_value, plan, harness = self._fixture(directory)
            trials = directory / "trials.jsonl"
            manifest = directory / "run.json"

            summary = self._run_with_manifest(
                plan=plan,
                harness=harness,
                trials=trials,
                manifest=manifest,
                failure_policy="continue",
            )

            sidecar = json.loads(manifest.read_text(encoding="utf-8"))
            records = [json.loads(line) for line in trials.read_text(encoding="utf-8").splitlines()]
            self.assertTrue(summary["completed_plan"])
            self.assertEqual(sidecar["planned_load_levels"], [1, 2, 4, 8])
            self.assertEqual(sidecar["tested_load_levels"], [1, 2, 4, 8])
            self.assertTrue(sidecar["completed_plan"])
            self.assertTrue(sidecar["boundary_found"])
            self.assertEqual(sidecar["plan_sha256"], RUNNER._canonical_digest(plan_value))
            self.assertEqual(sidecar["trials_sha256"], RUNNER._canonical_digest(records))
            self.assertEqual(sidecar["profile_label"], "720p30 3Mbps")
            self.assertEqual(sidecar["scenario_id"], "normal-input")
            self.assertEqual(sidecar["node_profile"], NODE_PROFILE)
            self.assertEqual(sidecar["software_revision"], SOFTWARE_REVISION)

    def test_stop_policy_records_partial_manifest_as_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            _plan_value, plan, harness = self._fixture(directory)
            trials = directory / "trials.jsonl"
            manifest = directory / "run.json"

            self._run_with_manifest(
                plan=plan,
                harness=harness,
                trials=trials,
                manifest=manifest,
                failure_policy="stop",
            )

            sidecar = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(sidecar["tested_load_levels"], [1, 2, 4])
            self.assertFalse(sidecar["completed_plan"])
            self.assertTrue(sidecar["boundary_found"])

    def test_missing_run_identity_fails_before_harness_or_trials(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            _plan_value, plan, harness = self._fixture(directory)
            trials = directory / "trials.jsonl"
            manifest = directory / "run.json"

            with self.assertRaisesRegex(RUNNER.ScenarioRunError, "valid node profile"):
                RUNNER.run_scenario(
                    plan_path=plan,
                    scenario_id="normal-input",
                    trials_jsonl=trials,
                    runner_command=[sys.executable, str(harness), "normal"],
                    timeout_seconds=2.0,
                    failure_policy="continue",
                    run_manifest=manifest,
                )
            self.assertFalse(trials.exists())
            self.assertFalse(manifest.exists())

    def test_invalid_software_revision_fails_before_harness(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            _plan_value, plan, harness = self._fixture(directory)
            trials = directory / "trials.jsonl"
            manifest = directory / "run.json"

            with self.assertRaisesRegex(RUNNER.ScenarioRunError, "software revision"):
                RUNNER.run_scenario(
                    plan_path=plan,
                    scenario_id="normal-input",
                    trials_jsonl=trials,
                    runner_command=[sys.executable, str(harness), "normal"],
                    timeout_seconds=2.0,
                    failure_policy="continue",
                    run_manifest=manifest,
                    node_profile=NODE_PROFILE,
                    software_revision="not-a-revision",
                )
            self.assertFalse(trials.exists())
            self.assertFalse(manifest.exists())

    def test_existing_manifest_fails_before_harness_or_trials(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            _plan_value, plan, harness = self._fixture(directory)
            trials = directory / "trials.jsonl"
            manifest = directory / "run.json"
            manifest.write_text("known-good\n", encoding="utf-8")

            with self.assertRaisesRegex(RUNNER.ScenarioRunError, "run manifest already exists"):
                self._run_with_manifest(
                    plan=plan,
                    harness=harness,
                    trials=trials,
                    manifest=manifest,
                    failure_policy="continue",
                )
            self.assertFalse(trials.exists())
            self.assertEqual(manifest.read_text(encoding="utf-8"), "known-good\n")

    def test_manifest_cannot_alias_trials_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            _plan_value, plan, harness = self._fixture(directory)
            evidence = directory / "same.json"

            with self.assertRaisesRegex(RUNNER.ScenarioRunError, "must not overwrite trials"):
                self._run_with_manifest(
                    plan=plan,
                    harness=harness,
                    trials=evidence,
                    manifest=evidence,
                    failure_policy="continue",
                )
            self.assertFalse(evidence.exists())

    def test_failed_harness_does_not_publish_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            _plan_value, plan, harness = self._fixture(directory)
            trials = directory / "trials.jsonl"
            manifest = directory / "run.json"

            with self.assertRaisesRegex(RUNNER.ScenarioRunError, "exited unsuccessfully"):
                self._run_with_manifest(
                    plan=plan,
                    harness=harness,
                    trials=trials,
                    manifest=manifest,
                    failure_policy="continue",
                    mode="nonzero",
                )
            self.assertFalse(trials.exists())
            self.assertFalse(manifest.exists())


if __name__ == "__main__":
    unittest.main()
