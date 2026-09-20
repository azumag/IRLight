from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


COVERAGE = _load(
    "node_capacity_provenance_coverage_validator_test",
    ROOT / "scripts" / "validate-node-capacity-coverage-manifest.py",
)
RENDER_COVERAGE = _load(
    "node_capacity_provenance_coverage_renderer_test",
    ROOT / "scripts" / "render-node-capacity-coverage-manifest.py",
)
PLAN_RENDERER = _load(
    "node_capacity_provenance_coverage_plan_renderer_test",
    ROOT / "scripts" / "render-node-capacity-load-plan.py",
)
PLANNED_ASSEMBLER = _load(
    "node_capacity_provenance_coverage_planned_assembler_test",
    ROOT / "scripts" / "assemble-node-capacity-planned-report.py",
)

PROFILE_LABEL = "720p30/1080p30 provenance qa-v2"
NODE_PROFILE = "linux-x86_64 4 vCPU 8 GiB"
SOFTWARE_REVISION = "1234567890abcdef1234567890abcdef12345678"
SCENARIO_IDS = [scenario_id for scenario_id, _description in PLAN_RENDERER.SCENARIOS]
LOAD_LEVELS = [1, 2, 4, 8]


def _trial(sessions: int) -> dict[str, object]:
    outcome = "pass" if sessions <= 4 else "fail"
    return {
        "concurrent_sessions": sessions,
        "duration_seconds": 300.0,
        "outcome": outcome,
        "cpu_peak_percent": min(95.0, float(10 + sessions * 10)),
        "memory_rss_peak_bytes": sessions * 100_000_000,
        "egress_peak_bps": float(sessions * 4_000_000),
        "failed_sessions": 0 if outcome == "pass" else 1,
        "unexpected_reconnects": 0,
    }


class NodeCapacityProvenanceCoverageTests(unittest.TestCase):
    def _fixture(self, root: pathlib.Path):
        evidence = root / "evidence"
        evidence.mkdir()
        plan_value = PLAN_RENDERER.build_plan(PROFILE_LABEL)
        plan_path = evidence / "plan.json"
        plan_path.write_text(json.dumps(plan_value, sort_keys=True), encoding="utf-8")

        entries: list[dict[str, str]] = []
        report_bindings: list[tuple[str, str]] = []
        trials_bindings: list[tuple[str, str]] = []
        manifest_bindings: list[tuple[str, str]] = []
        for index, scenario_id in enumerate(SCENARIO_IDS, start=1):
            records = [_trial(level) for level in LOAD_LEVELS]
            trials_path = evidence / f"{scenario_id}.trials.jsonl"
            trials_path.write_text(
                "\n".join(json.dumps(record, separators=(",", ":")) for record in records)
                + "\n",
                encoding="utf-8",
            )
            run_manifest_value = {
                "schema_version": 1,
                "plan_sha256": PLANNED_ASSEMBLER._canonical_digest(plan_value),
                "profile_label": PROFILE_LABEL,
                "scenario_id": scenario_id,
                "node_profile": NODE_PROFILE,
                "software_revision": SOFTWARE_REVISION,
                "failure_policy": "continue",
                "planned_load_levels": LOAD_LEVELS,
                "tested_load_levels": LOAD_LEVELS,
                "boundary_found": True,
                "completed_plan": True,
                "trials_sha256": PLANNED_ASSEMBLER._canonical_digest(records),
            }
            run_manifest_path = evidence / f"{scenario_id}.run.json"
            run_manifest_path.write_text(
                json.dumps(run_manifest_value, sort_keys=True), encoding="utf-8"
            )
            report_value = PLANNED_ASSEMBLER.assemble_planned_report(
                plan_path=plan_path,
                trials_path=trials_path,
                run_manifest_path=run_manifest_path,
                scenario_id=scenario_id,
                run_id=str(uuid.UUID(int=index)),
                safety_margin_percent=25,
                notes="provenance coverage fixture",
            )
            report_path = evidence / f"{scenario_id}.report.json"
            report_path.write_text(
                json.dumps(report_value, sort_keys=True), encoding="utf-8"
            )

            report_name = report_path.relative_to(root).as_posix()
            trials_name = trials_path.relative_to(root).as_posix()
            manifest_name = run_manifest_path.relative_to(root).as_posix()
            entries.append(
                {
                    "scenario_id": scenario_id,
                    "path": report_name,
                    "trials_path": trials_name,
                    "run_manifest_path": manifest_name,
                }
            )
            report_bindings.append((scenario_id, report_name))
            trials_bindings.append((scenario_id, trials_name))
            manifest_bindings.append((scenario_id, manifest_name))

        payload = {
            "schema_version": 2,
            "load_plan": plan_path.relative_to(root).as_posix(),
            "reports": entries,
        }
        return payload, report_bindings, trials_bindings, manifest_bindings

    def test_schema_v2_revalidates_every_report_against_run_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            payload, _reports, _trials, _manifests = self._fixture(root)
            summary = COVERAGE.validate_manifest(payload, repo_root=root)

        self.assertTrue(summary["valid"])
        self.assertEqual(summary["schema_version"], 2)
        self.assertTrue(summary["provenance_bound"])
        self.assertEqual(summary["scenario_ids"], SCENARIO_IDS)
        self.assertEqual(summary["report_count"], len(SCENARIO_IDS))
        self.assertEqual(summary["software_revision"], SOFTWARE_REVISION)

    def test_schema_v1_remains_compatible_but_is_explicitly_not_provenance_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            payload, _reports, _trials, _manifests = self._fixture(root)
            payload["schema_version"] = 1
            payload["reports"] = [
                {"scenario_id": entry["scenario_id"], "path": entry["path"]}
                for entry in payload["reports"]
            ]
            summary = COVERAGE.validate_manifest(payload, repo_root=root)

        self.assertEqual(summary["schema_version"], 1)
        self.assertFalse(summary["provenance_bound"])

    def test_raw_trial_tamper_invalidates_schema_v2_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            payload, _reports, _trials, _manifests = self._fixture(root)
            trials_path = root / payload["reports"][0]["trials_path"]
            records = [json.loads(line) for line in trials_path.read_text().splitlines()]
            records[0]["cpu_peak_percent"] = 99.0
            trials_path.write_text(
                "\n".join(json.dumps(record) for record in records) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                COVERAGE.CapacityCoverageManifestError,
                "canonical run provenance",
            ):
                COVERAGE.validate_manifest(payload, repo_root=root)

    def test_cross_scenario_run_manifest_binding_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            payload, _reports, _trials, _manifests = self._fixture(root)
            payload["reports"][0]["run_manifest_path"] = payload["reports"][1][
                "run_manifest_path"
            ]
            with self.assertRaises(COVERAGE.CapacityCoverageManifestError):
                COVERAGE.validate_manifest(payload, repo_root=root)

    def test_duplicate_provenance_path_is_rejected_before_downstream_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            payload, _reports, _trials, _manifests = self._fixture(root)
            payload["reports"][1]["trials_path"] = payload["reports"][0]["trials_path"]
            with self.assertRaisesRegex(
                COVERAGE.CapacityCoverageManifestError,
                "duplicate provenance evidence binding",
            ):
                COVERAGE.validate_manifest(payload, repo_root=root)

    def test_renderer_emits_schema_v2_only_with_complete_provenance_bindings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            payload, reports, trials, manifests = self._fixture(root)
            rendered = RENDER_COVERAGE.render_manifest(
                payload["load_plan"],
                reports,
                trials_bindings=trials,
                run_manifest_bindings=manifests,
                repo_root=root,
            )
            self.assertEqual(rendered, payload)

            with self.assertRaisesRegex(
                RENDER_COVERAGE.CoverageManifestRenderError,
                "exactly match report scenarios",
            ):
                RENDER_COVERAGE.render_manifest(
                    payload["load_plan"],
                    reports,
                    trials_bindings=trials[:-1],
                    run_manifest_bindings=manifests,
                    repo_root=root,
                )


if __name__ == "__main__":
    unittest.main()
