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
    "node_capacity_review_bundle_host_renderer_test",
    ROOT / "scripts" / "render-node-capacity-review-bundle.py",
)
BUNDLE_VALIDATOR = _load(
    "node_capacity_review_bundle_host_validator_test",
    ROOT / "scripts" / "validate-node-capacity-review-bundle.py",
)
BUNDLE_WRITER = _load(
    "node_capacity_review_bundle_host_writer_test",
    ROOT / "scripts" / "write-node-capacity-review-bundle.py",
)
HOST_RENDERER = _load(
    "node_capacity_review_bundle_host_sidecar_renderer_test",
    ROOT / "scripts" / "render-node-capacity-host-provenance.py",
)
PROPOSAL_RENDERER = _load(
    "node_capacity_review_bundle_host_proposal_renderer_test",
    ROOT / "scripts" / "render-node-capacity-max-sessions-proposal.py",
)
PLAN_RENDERER = _load(
    "node_capacity_review_bundle_host_plan_renderer_test",
    ROOT / "scripts" / "render-node-capacity-load-plan.py",
)
PLANNED_ASSEMBLER = _load(
    "node_capacity_review_bundle_host_planned_assembler_test",
    ROOT / "scripts" / "assemble-node-capacity-planned-report.py",
)

PROFILE_LABEL = "720p30/1080p30 review host provenance-v3"
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


def _preflight(*, cpu_count: int = 8) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "irlight-node-capacity-host-preflight",
        "ready": True,
        "platform": {
            "system": "Linux",
            "machine": "x86_64",
            "kernel_release": "6.8.0-test",
        },
        "resources": {
            "logical_cpu_count": cpu_count,
            "memory_total_bytes": 16 * 1024 * 1024 * 1024,
        },
        "docker": {
            "server_version": "28.0.1",
            "compose_version": "2.33.1",
        },
    }


class NodeCapacityReviewBundleHostProvenanceTests(unittest.TestCase):
    def _fixture(
        self,
        root: pathlib.Path,
    ) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path, pathlib.Path]:
        evidence = root / "evidence"
        evidence.mkdir()
        plan_value = PLAN_RENDERER.build_plan(PROFILE_LABEL)
        plan_path = evidence / "plan.json"
        plan_path.write_text(json.dumps(plan_value, sort_keys=True) + "\n", encoding="utf-8")

        entries: list[dict[str, str]] = []
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
                notes="review bundle host provenance fixture",
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
            expected_software_revision=SOFTWARE_REVISION,
            repo_root=root,
        )
        proposal_path = evidence / "proposal.json"
        proposal_path.write_text(
            json.dumps(proposal, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        preflight_path = evidence / "host-preflight.json"
        preflight_path.write_text(
            json.dumps(_preflight(), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        preflight_name = preflight_path.relative_to(root).as_posix()
        sidecar = HOST_RENDERER.render_manifest(
            coverage_path.relative_to(root).as_posix(),
            [(scenario_id, preflight_name) for scenario_id in SCENARIO_IDS],
            repo_root=root,
        )
        sidecar_path = evidence / "host-provenance.json"
        sidecar_path.write_text(
            json.dumps(sidecar, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return proposal_path, coverage_path, sidecar_path, preflight_path

    def _bundle(
        self,
        root: pathlib.Path,
    ) -> tuple[dict[str, object], pathlib.Path, pathlib.Path, pathlib.Path]:
        proposal_path, coverage_path, sidecar_path, preflight_path = self._fixture(root)
        bundle = BUNDLE_RENDERER.render_bundle(
            proposal_path,
            expected_node_profile=NODE_PROFILE,
            expected_software_revision=SOFTWARE_REVISION,
            host_provenance_path=sidecar_path.relative_to(root),
            repo_root=root,
        )
        return bundle, coverage_path, sidecar_path, preflight_path

    def test_schema_v3_pins_sidecar_and_exposes_exact_preflight_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            bundle, _coverage, sidecar_path, preflight_path = self._bundle(root)
            bundle_path = root / "evidence" / "review-bundle.json"
            bundle_path.write_text(json.dumps(bundle, sort_keys=True) + "\n", encoding="utf-8")
            validated = BUNDLE_VALIDATOR.validate_bundle_file(
                bundle_path,
                expected_node_profile=NODE_PROFILE,
                expected_software_revision=SOFTWARE_REVISION,
                repo_root=root,
            )

        self.assertEqual(validated, bundle)
        self.assertEqual(bundle["schema_version"], 3)
        host = bundle["host_provenance"]
        self.assertEqual(host["path"], sidecar_path.relative_to(root).as_posix())
        self.assertRegex(str(host["sha256"]), r"^[0-9a-f]{64}$")
        self.assertEqual(
            [entry["scenario_id"] for entry in host["scenarios"]],
            SCENARIO_IDS,
        )
        self.assertEqual(
            {entry["host_preflight"]["path"] for entry in host["scenarios"]},
            {preflight_path.relative_to(root).as_posix()},
        )
        for entry in host["scenarios"]:
            self.assertRegex(str(entry["host_preflight"]["sha256"]), r"^[0-9a-f]{64}$")

    def test_host_preflight_byte_change_invalidates_v3_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            bundle, _coverage, _sidecar, preflight_path = self._bundle(root)
            bundle_path = root / "evidence" / "review-bundle.json"
            bundle_path.write_text(json.dumps(bundle, sort_keys=True) + "\n", encoding="utf-8")
            preflight_path.write_text(
                json.dumps(_preflight(cpu_count=9), sort_keys=True) + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(BUNDLE_VALIDATOR.CapacityReviewBundleValidationError):
                BUNDLE_VALIDATOR.validate_bundle_file(
                    bundle_path,
                    expected_node_profile=NODE_PROFILE,
                    expected_software_revision=SOFTWARE_REVISION,
                    repo_root=root,
                )

    def test_semantically_equivalent_sidecar_byte_change_invalidates_v3_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            bundle, _coverage, sidecar_path, _preflight = self._bundle(root)
            bundle_path = root / "evidence" / "review-bundle.json"
            bundle_path.write_text(json.dumps(bundle, sort_keys=True) + "\n", encoding="utf-8")
            sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
            sidecar_path.write_text(
                json.dumps(sidecar, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                BUNDLE_VALIDATOR.CapacityReviewBundleValidationError,
                "does not match current pinned capacity evidence",
            ):
                BUNDLE_VALIDATOR.validate_bundle_file(
                    bundle_path,
                    expected_node_profile=NODE_PROFILE,
                    expected_software_revision=SOFTWARE_REVISION,
                    repo_root=root,
                )

    def test_scenario_preflight_mapping_tamper_invalidates_v3_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            bundle, _coverage, _sidecar, _preflight = self._bundle(root)
            bundle["host_provenance"]["scenarios"][0]["host_preflight"]["sha256"] = "0" * 64
            bundle_path = root / "evidence" / "review-bundle.json"
            bundle_path.write_text(json.dumps(bundle, sort_keys=True) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(
                BUNDLE_VALIDATOR.CapacityReviewBundleValidationError,
                "does not match current pinned capacity evidence",
            ):
                BUNDLE_VALIDATOR.validate_bundle_file(
                    bundle_path,
                    expected_node_profile=NODE_PROFILE,
                    expected_software_revision=SOFTWARE_REVISION,
                    repo_root=root,
                )

    def test_renderer_rejects_sidecar_for_different_coverage_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            proposal_path, coverage_path, _sidecar_path, preflight_path = self._fixture(root)
            alternate_coverage = root / "evidence" / "coverage-copy.json"
            alternate_coverage.write_bytes(coverage_path.read_bytes())
            preflight_name = preflight_path.relative_to(root).as_posix()
            sidecar = HOST_RENDERER.render_manifest(
                alternate_coverage.relative_to(root).as_posix(),
                [(scenario_id, preflight_name) for scenario_id in SCENARIO_IDS],
                repo_root=root,
            )
            alternate_sidecar = root / "evidence" / "host-provenance-copy.json"
            alternate_sidecar.write_text(
                json.dumps(sidecar, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                BUNDLE_RENDERER.CapacityReviewBundleRenderError,
                "does not match proposal coverage",
            ):
                BUNDLE_RENDERER.render_bundle(
                    proposal_path,
                    expected_node_profile=NODE_PROFILE,
                    expected_software_revision=SOFTWARE_REVISION,
                    host_provenance_path=alternate_sidecar.relative_to(root),
                    repo_root=root,
                )

    def test_atomic_writer_refuses_to_replace_sidecar_or_host_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            bundle, _coverage, sidecar_path, preflight_path = self._bundle(root)
            for target in (sidecar_path, preflight_path):
                original = target.read_bytes()
                with self.subTest(target=target.name), self.assertRaisesRegex(
                    BUNDLE_WRITER.CapacityReviewBundleWriteError,
                    "must not replace pinned capacity evidence",
                ):
                    BUNDLE_WRITER.write_bundle_atomically(
                        bundle,
                        target.relative_to(root),
                        repo_root=root,
                    )
                self.assertEqual(target.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
