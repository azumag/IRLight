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


BUNDLE_RENDERER = _load(
    "node_capacity_review_bundle_provenance_renderer_test",
    ROOT / "scripts" / "render-node-capacity-review-bundle.py",
)
BUNDLE_VALIDATOR = _load(
    "node_capacity_review_bundle_provenance_validator_test",
    ROOT / "scripts" / "validate-node-capacity-review-bundle.py",
)
BUNDLE_WRITER = _load(
    "node_capacity_review_bundle_provenance_writer_test",
    ROOT / "scripts" / "write-node-capacity-review-bundle.py",
)
PROPOSAL_RENDERER = _load(
    "node_capacity_review_bundle_provenance_proposal_renderer_test",
    ROOT / "scripts" / "render-node-capacity-max-sessions-proposal.py",
)
PLAN_RENDERER = _load(
    "node_capacity_review_bundle_provenance_plan_renderer_test",
    ROOT / "scripts" / "render-node-capacity-load-plan.py",
)
PLANNED_ASSEMBLER = _load(
    "node_capacity_review_bundle_provenance_planned_assembler_test",
    ROOT / "scripts" / "assemble-node-capacity-planned-report.py",
)

PROFILE = "720p30/1080p30 review provenance-v2"
NODE_PROFILE = "linux-x86_64 4 vCPU 8 GiB"
REVISION = "1234567890abcdef1234567890abcdef12345678"
LOAD_LEVELS = [1, 2, 4, 8]
SCENARIO_IDS = [scenario_id for scenario_id, _description in PLAN_RENDERER.SCENARIOS]


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


class NodeCapacityReviewBundleProvenanceTests(unittest.TestCase):
    def _write_v2_evidence(
        self,
        root: pathlib.Path,
    ) -> tuple[pathlib.Path, dict[str, pathlib.Path]]:
        evidence = root / "evidence"
        evidence.mkdir()
        plan_value = PLAN_RENDERER.build_plan(PROFILE)
        plan_path = evidence / "plan.json"
        plan_path.write_text(json.dumps(plan_value, sort_keys=True) + "\n", encoding="utf-8")

        entries: list[dict[str, str]] = []
        paths: dict[str, pathlib.Path] = {"plan": plan_path}
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
                "profile_label": PROFILE,
                "scenario_id": scenario_id,
                "node_profile": NODE_PROFILE,
                "software_revision": REVISION,
                "failure_policy": "continue",
                "planned_load_levels": LOAD_LEVELS,
                "tested_load_levels": LOAD_LEVELS,
                "boundary_found": True,
                "completed_plan": True,
                "trials_sha256": PLANNED_ASSEMBLER._canonical_digest(records),
            }
            run_manifest_path = evidence / f"{scenario_id}.run.json"
            run_manifest_path.write_text(
                json.dumps(run_manifest_value, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            report_value = PLANNED_ASSEMBLER.assemble_planned_report(
                plan_path=plan_path,
                trials_path=trials_path,
                run_manifest_path=run_manifest_path,
                scenario_id=scenario_id,
                run_id=str(uuid.UUID(int=index)),
                safety_margin_percent=25,
                notes="review bundle provenance fixture",
            )
            report_path = evidence / f"{scenario_id}.report.json"
            report_path.write_text(
                json.dumps(report_value, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            entries.append(
                {
                    "scenario_id": scenario_id,
                    "path": report_path.relative_to(root).as_posix(),
                    "trials_path": trials_path.relative_to(root).as_posix(),
                    "run_manifest_path": run_manifest_path.relative_to(root).as_posix(),
                }
            )
            if index == 1:
                paths.update(
                    {
                        "report": report_path,
                        "trials": trials_path,
                        "run_manifest": run_manifest_path,
                    }
                )

        coverage_path = evidence / "coverage.json"
        coverage_path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "load_plan": plan_path.relative_to(root).as_posix(),
                    "reports": entries,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        proposal = PROPOSAL_RENDERER.render_proposal(
            coverage_path,
            3,
            expected_node_profile=NODE_PROFILE,
            expected_software_revision=REVISION,
            repo_root=root,
        )
        proposal_path = evidence / "proposal.json"
        proposal_path.write_text(
            json.dumps(proposal, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        paths.update({"coverage": coverage_path, "proposal": proposal_path})
        return proposal_path, paths

    def _write_bundle(
        self,
        root: pathlib.Path,
    ) -> tuple[pathlib.Path, dict[str, object], dict[str, pathlib.Path]]:
        proposal_path, paths = self._write_v2_evidence(root)
        bundle = BUNDLE_RENDERER.render_bundle(
            proposal_path,
            expected_node_profile=NODE_PROFILE,
            expected_software_revision=REVISION,
            repo_root=root,
        )
        bundle_path = root / "evidence" / "review-bundle.json"
        bundle_path.write_text(json.dumps(bundle, sort_keys=True) + "\n", encoding="utf-8")
        return bundle_path, bundle, paths

    def test_v2_bundle_pins_raw_trials_and_run_manifests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            bundle_path, bundle, _paths = self._write_bundle(root)
            validated = BUNDLE_VALIDATOR.validate_bundle_file(
                bundle_path,
                expected_node_profile=NODE_PROFILE,
                expected_software_revision=REVISION,
                repo_root=root,
            )

        self.assertEqual(validated, bundle)
        self.assertEqual(bundle["schema_version"], 2)
        self.assertEqual(len(bundle["reports"]), len(SCENARIO_IDS))
        for report in bundle["reports"]:
            self.assertRegex(str(report["sha256"]), r"^[0-9a-f]{64}$")
            self.assertRegex(str(report["trials_sha256"]), r"^[0-9a-f]{64}$")
            self.assertRegex(str(report["run_manifest_sha256"]), r"^[0-9a-f]{64}$")
            self.assertTrue(str(report["trials_path"]).endswith(".trials.jsonl"))
            self.assertTrue(str(report["run_manifest_path"]).endswith(".run.json"))

    def test_semantically_equivalent_raw_provenance_byte_change_invalidates_bundle(self) -> None:
        for target_name in ("trials", "run_manifest"):
            with self.subTest(target=target_name), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                bundle_path, _bundle, paths = self._write_bundle(root)
                target = paths[target_name]
                original = target.read_text(encoding="utf-8")
                if target_name == "trials":
                    changed = "\n".join(
                        line + " " for line in original.splitlines()
                    ) + "\n"
                    self.assertEqual(
                        [json.loads(line) for line in original.splitlines()],
                        [json.loads(line) for line in changed.splitlines()],
                    )
                else:
                    changed = original.rstrip("\n") + "  \n"
                    self.assertEqual(json.loads(original), json.loads(changed))
                target.write_text(changed, encoding="utf-8")
                with self.assertRaises(
                    BUNDLE_VALIDATOR.CapacityReviewBundleValidationError
                ):
                    BUNDLE_VALIDATOR.validate_bundle_file(
                        bundle_path,
                        expected_node_profile=NODE_PROFILE,
                        expected_software_revision=REVISION,
                        repo_root=root,
                    )

    def test_atomic_writer_refuses_to_replace_raw_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            _bundle_path, bundle, paths = self._write_bundle(root)
            for target_name in ("trials", "run_manifest"):
                target = paths[target_name]
                original = target.read_bytes()
                with self.subTest(target=target_name), self.assertRaisesRegex(
                    BUNDLE_WRITER.CapacityReviewBundleWriteError,
                    "must not replace pinned capacity evidence",
                ):
                    BUNDLE_WRITER.write_bundle_atomically(
                        bundle,
                        target.relative_to(root),
                        repo_root=root,
                    )
                self.assertEqual(target.read_bytes(), original)

    def test_v2_bundle_rejects_downgraded_schema_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            bundle_path, bundle, _paths = self._write_bundle(root)
            downgraded = dict(bundle)
            downgraded["schema_version"] = 1
            bundle_path.write_text(json.dumps(downgraded), encoding="utf-8")
            with self.assertRaisesRegex(
                BUNDLE_VALIDATOR.CapacityReviewBundleValidationError,
                "does not match current pinned capacity evidence",
            ):
                BUNDLE_VALIDATOR.validate_bundle_file(
                    bundle_path,
                    expected_node_profile=NODE_PROFILE,
                    expected_software_revision=REVISION,
                    repo_root=root,
                )


if __name__ == "__main__":
    unittest.main()
