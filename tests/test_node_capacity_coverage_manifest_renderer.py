from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "render-node-capacity-coverage-manifest.py"
SPEC = importlib.util.spec_from_file_location("node_capacity_coverage_manifest_renderer", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

RENDERER_PATH = ROOT / "scripts" / "render-node-capacity-load-plan.py"
RENDERER_SPEC = importlib.util.spec_from_file_location(
    "node_capacity_coverage_manifest_renderer_test_plan", RENDERER_PATH
)
assert RENDERER_SPEC is not None and RENDERER_SPEC.loader is not None
PLAN_RENDERER = importlib.util.module_from_spec(RENDERER_SPEC)
sys.modules[RENDERER_SPEC.name] = PLAN_RENDERER
RENDERER_SPEC.loader.exec_module(PLAN_RENDERER)

PROFILE = "720p30/1080p30 mix qa-v1"
REVISION = "0123456789abcdef0123456789abcdef01234567"
SCENARIO_IDS = [scenario_id for scenario_id, _description in PLAN_RENDERER.SCENARIOS]


def make_report(scenario_id: str, run_number: int) -> dict[str, object]:
    trials = []
    for sessions in (1, 2, 4, 8):
        outcome = "fail" if sessions == 8 else "pass"
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
        "node_profile": "linux-x86_64 4 vCPU 8 GiB",
        "software_revision": REVISION,
        "scenario": f"profile={PROFILE}; scenario={scenario_id}; approved test policy",
        "safety_margin_percent": 25,
        "trials": trials,
        "notes": "synthetic renderer fixture",
    }


class NodeCapacityCoverageManifestRendererTests(unittest.TestCase):
    def write_evidence(
        self, root: pathlib.Path
    ) -> tuple[str, list[tuple[str, str]]]:
        evidence = root / "evidence"
        evidence.mkdir()
        plan_path = evidence / "plan.json"
        plan_path.write_text(
            json.dumps(PLAN_RENDERER.build_plan(PROFILE), sort_keys=True),
            encoding="utf-8",
        )
        bindings: list[tuple[str, str]] = []
        for index, scenario_id in enumerate(SCENARIO_IDS, start=1):
            report_path = evidence / f"{scenario_id}.json"
            report_path.write_text(
                json.dumps(make_report(scenario_id, index), sort_keys=True),
                encoding="utf-8",
            )
            bindings.append(
                (scenario_id, report_path.relative_to(root).as_posix())
            )
        return plan_path.relative_to(root).as_posix(), bindings

    def test_renderer_emits_validator_accepted_manifest_in_plan_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            plan, bindings = self.write_evidence(root)
            manifest = MODULE.render_manifest(plan, reversed(bindings), repo_root=root)

        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["load_plan"], plan)
        self.assertEqual(
            [entry["scenario_id"] for entry in manifest["reports"]],
            SCENARIO_IDS,
        )
        self.assertEqual(
            [entry["path"] for entry in manifest["reports"]],
            [path for _scenario_id, path in bindings],
        )

    def test_missing_scenario_fails_without_reflecting_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            plan, bindings = self.write_evidence(root)
            malicious_path = "evidence/report\x1b[31m.json"
            broken = list(bindings[:-1])
            broken[0] = (broken[0][0], malicious_path)
            with self.assertRaises(MODULE.CoverageManifestRenderError) as context:
                MODULE.render_manifest(plan, broken, repo_root=root)

        self.assertEqual(str(context.exception), "coverage inputs are invalid")
        self.assertNotIn("\x1b", str(context.exception))
        self.assertNotIn(malicious_path, str(context.exception))

    def test_duplicate_binding_is_rejected_without_echoing_scenario(self) -> None:
        malicious_id = "normal-input\x1b[31m"
        with self.assertRaises(MODULE.CoverageManifestRenderError) as context:
            MODULE.normalize_report_bindings(
                [(malicious_id, "a.json"), (malicious_id, "b.json")]
            )
        self.assertEqual(str(context.exception), "duplicate report binding for scenario")
        self.assertNotIn("\x1b", str(context.exception))
        self.assertNotIn(malicious_id, str(context.exception))

    def test_noncanonical_or_escaping_repository_paths_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            plan, bindings = self.write_evidence(root)
            with self.assertRaisesRegex(
                MODULE.CoverageManifestRenderError, "coverage inputs are invalid"
            ):
                MODULE.render_manifest("../outside.json", bindings, repo_root=root)
            with self.assertRaisesRegex(
                MODULE.CoverageManifestRenderError, "coverage inputs are invalid"
            ):
                MODULE.render_manifest(plan, [(bindings[0][0], "./evidence/x.json")], repo_root=root)

    def test_report_binding_parser_requires_canonical_one_line_input(self) -> None:
        self.assertEqual(
            MODULE.parse_report_binding("normal-input=evidence/report.json"),
            ("normal-input", "evidence/report.json"),
        )
        for raw in (
            "normal-input",
            " normal-input=evidence/report.json",
            "normal-input=evidence/report.json ",
            "normal-input=evidence/report\n.json",
        ):
            with self.assertRaises(Exception):
                MODULE.parse_report_binding(raw)

    def test_renderer_has_no_load_execution_or_network_imports(self) -> None:
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
