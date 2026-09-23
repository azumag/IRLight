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


VALIDATOR = _load(
    "node_capacity_host_provenance_validator_test",
    ROOT / "scripts" / "validate-node-capacity-host-provenance.py",
)
RENDERER = _load(
    "node_capacity_host_provenance_renderer_test",
    ROOT / "scripts" / "render-node-capacity-host-provenance.py",
)
PLAN_RENDERER = _load(
    "node_capacity_host_provenance_plan_renderer_test",
    ROOT / "scripts" / "render-node-capacity-load-plan.py",
)
PLANNED_ASSEMBLER = _load(
    "node_capacity_host_provenance_planned_assembler_test",
    ROOT / "scripts" / "assemble-node-capacity-planned-report.py",
)

PROFILE_LABEL = "720p30/1080p30 host-provenance qa"
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


def _preflight() -> dict[str, object]:
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
            "logical_cpu_count": 8,
            "memory_total_bytes": 16 * 1024 * 1024 * 1024,
        },
        "docker": {
            "server_version": "28.0.1",
            "compose_version": "2.33.1",
        },
    }


class NodeCapacityHostProvenanceTests(unittest.TestCase):
    def _fixture(self, root: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
        evidence = root / "evidence"
        evidence.mkdir()
        plan_value = PLAN_RENDERER.build_plan(PROFILE_LABEL)
        plan_path = evidence / "plan.json"
        plan_path.write_text(json.dumps(plan_value, sort_keys=True), encoding="utf-8")

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
                json.dumps(run_manifest_value, sort_keys=True), encoding="utf-8"
            )
            report_value = PLANNED_ASSEMBLER.assemble_planned_report(
                plan_path=plan_path,
                trials_path=trials_path,
                run_manifest_path=run_manifest_path,
                scenario_id=scenario_id,
                run_id=str(uuid.UUID(int=index)),
                safety_margin_percent=25,
                notes="host provenance fixture",
            )
            report_path = evidence / f"{scenario_id}.report.json"
            report_path.write_text(json.dumps(report_value, sort_keys=True), encoding="utf-8")
            entries.append(
                {
                    "scenario_id": scenario_id,
                    "path": report_path.relative_to(root).as_posix(),
                    "trials_path": trials_path.relative_to(root).as_posix(),
                    "run_manifest_path": run_manifest_path.relative_to(root).as_posix(),
                }
            )

        coverage = {
            "schema_version": 2,
            "load_plan": plan_path.relative_to(root).as_posix(),
            "reports": entries,
        }
        coverage_path = evidence / "coverage.json"
        coverage_path.write_text(json.dumps(coverage, sort_keys=True), encoding="utf-8")

        preflight_path = evidence / "host-preflight.json"
        preflight_path.write_text(json.dumps(_preflight(), sort_keys=True), encoding="utf-8")
        return coverage_path, preflight_path

    def _bindings(
        self,
        root: pathlib.Path,
        preflight_path: pathlib.Path,
    ) -> list[tuple[str, str]]:
        relative = preflight_path.relative_to(root).as_posix()
        return [(scenario_id, relative) for scenario_id in SCENARIO_IDS]

    def test_renderer_and_validator_bind_complete_closure_with_shared_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            coverage_path, preflight_path = self._fixture(root)
            payload = RENDERER.render_manifest(
                coverage_path.relative_to(root).as_posix(),
                self._bindings(root, preflight_path),
                repo_root=root,
            )
            summary = VALIDATOR.validate_manifest(payload, repo_root=root)

        self.assertTrue(summary["valid"])
        self.assertTrue(summary["host_provenance_bound"])
        self.assertEqual(summary["scenario_ids"], SCENARIO_IDS)
        self.assertEqual(summary["scenario_count"], len(SCENARIO_IDS))
        self.assertEqual(
            {entry["host_preflight"]["path"] for entry in payload["scenarios"]},
            {"evidence/host-preflight.json"},
        )

    def test_distinct_preflight_per_scenario_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            coverage_path, _shared = self._fixture(root)
            bindings: list[tuple[str, str]] = []
            for index, scenario_id in enumerate(SCENARIO_IDS):
                path = root / "evidence" / f"host-preflight-{index}.json"
                snapshot = _preflight()
                snapshot["resources"] = {
                    "logical_cpu_count": 8 + index,
                    "memory_total_bytes": (16 + index) * 1024 * 1024 * 1024,
                }
                path.write_text(json.dumps(snapshot, sort_keys=True), encoding="utf-8")
                bindings.append((scenario_id, path.relative_to(root).as_posix()))

            payload = RENDERER.render_manifest(
                coverage_path.relative_to(root).as_posix(),
                bindings,
                repo_root=root,
            )
            summary = VALIDATOR.validate_manifest(payload, repo_root=root)

        self.assertTrue(summary["valid"])
        self.assertEqual(
            len({entry["host_preflight"]["path"] for entry in payload["scenarios"]}),
            len(SCENARIO_IDS),
        )

    def test_host_preflight_tamper_invalidates_digest_closure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            coverage_path, preflight_path = self._fixture(root)
            payload = RENDERER.render_manifest(
                coverage_path.relative_to(root).as_posix(),
                self._bindings(root, preflight_path),
                repo_root=root,
            )
            changed = _preflight()
            changed["docker"] = {
                "server_version": "28.0.2",
                "compose_version": "2.33.1",
            }
            preflight_path.write_text(json.dumps(changed, sort_keys=True), encoding="utf-8")
            with self.assertRaisesRegex(VALIDATOR.HostProvenanceError, "host preflight digest"):
                VALIDATOR.validate_manifest(payload, repo_root=root)

    def test_report_tamper_invalidates_digest_even_when_json_remains_valid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            coverage_path, preflight_path = self._fixture(root)
            payload = RENDERER.render_manifest(
                coverage_path.relative_to(root).as_posix(),
                self._bindings(root, preflight_path),
                repo_root=root,
            )
            report = root / payload["scenarios"][0]["report"]["path"]
            report.write_text(report.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            with self.assertRaisesRegex(VALIDATOR.HostProvenanceError, "report evidence digest"):
                VALIDATOR.validate_manifest(payload, repo_root=root)

    def test_unknown_secret_bearing_preflight_field_is_rejected_before_pinning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            coverage_path, preflight_path = self._fixture(root)
            snapshot = _preflight()
            snapshot["credential"] = "must-not-enter-provenance"
            preflight_path.write_text(json.dumps(snapshot), encoding="utf-8")
            with self.assertRaisesRegex(
                RENDERER.HostProvenanceRenderError,
                "host provenance inputs are invalid",
            ):
                RENDERER.render_manifest(
                    coverage_path.relative_to(root).as_posix(),
                    self._bindings(root, preflight_path),
                    repo_root=root,
                )

    def test_duplicate_key_nonfinite_symlink_and_oversize_preflight_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            coverage_path, preflight_path = self._fixture(root)
            coverage_name = coverage_path.relative_to(root).as_posix()

            invalid_payloads = (
                '{"schema_version":1,"schema_version":1}\n',
                '{"schema_version":1,"kind":"irlight-node-capacity-host-preflight","ready":true,"platform":{},"resources":{},"docker":{"server_version":NaN}}\n',
                json.dumps({"padding": "x" * (70 * 1024)}),
            )
            for raw in invalid_payloads:
                with self.subTest(kind=raw[:32]):
                    preflight_path.write_text(raw, encoding="utf-8")
                    with self.assertRaises(RENDERER.HostProvenanceRenderError):
                        RENDERER.render_manifest(
                            coverage_name,
                            self._bindings(root, preflight_path),
                            repo_root=root,
                        )

            preflight_path.write_text(json.dumps(_preflight()), encoding="utf-8")
            symlink = root / "evidence" / "host-preflight-link.json"
            try:
                symlink.symlink_to(preflight_path.name)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation is unavailable")
            with self.assertRaises(RENDERER.HostProvenanceRenderError):
                RENDERER.render_manifest(
                    coverage_name,
                    self._bindings(root, symlink),
                    repo_root=root,
                )

    def test_coverage_v1_is_not_silently_upgraded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            coverage_path, preflight_path = self._fixture(root)
            coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
            coverage["schema_version"] = 1
            coverage["reports"] = [
                {"scenario_id": entry["scenario_id"], "path": entry["path"]}
                for entry in coverage["reports"]
            ]
            coverage_path.write_text(json.dumps(coverage, sort_keys=True), encoding="utf-8")
            with self.assertRaisesRegex(
                RENDERER.HostProvenanceRenderError,
                "schema v2",
            ):
                RENDERER.render_manifest(
                    coverage_path.relative_to(root).as_posix(),
                    self._bindings(root, preflight_path),
                    repo_root=root,
                )


if __name__ == "__main__":
    unittest.main()
