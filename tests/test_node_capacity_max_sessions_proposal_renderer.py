from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import unittest
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "render-node-capacity-max-sessions-proposal.py"
SPEC = importlib.util.spec_from_file_location(
    "node_capacity_max_sessions_proposal_renderer", SCRIPT
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

PLAN_RENDERER_PATH = ROOT / "scripts" / "render-node-capacity-load-plan.py"
PLAN_RENDERER_SPEC = importlib.util.spec_from_file_location(
    "node_capacity_proposal_test_plan_renderer", PLAN_RENDERER_PATH
)
assert PLAN_RENDERER_SPEC is not None and PLAN_RENDERER_SPEC.loader is not None
PLAN_RENDERER = importlib.util.module_from_spec(PLAN_RENDERER_SPEC)
sys.modules[PLAN_RENDERER_SPEC.name] = PLAN_RENDERER
PLAN_RENDERER_SPEC.loader.exec_module(PLAN_RENDERER)

PROFILE = "720p30/1080p30 mix qa-v1"
NODE_PROFILE = "linux-x86_64 4 vCPU 8 GiB"
REVISION = "0123456789abcdef0123456789abcdef01234567"
SCENARIO_IDS = [scenario_id for scenario_id, _description in PLAN_RENDERER.SCENARIOS]


def make_report(scenario_id: str, run_number: int) -> dict[str, object]:
    trials: list[dict[str, object]] = []
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
        "node_profile": NODE_PROFILE,
        "software_revision": REVISION,
        "scenario": f"profile={PROFILE}; scenario={scenario_id}; approved test policy",
        "safety_margin_percent": 25,
        "trials": trials,
        "notes": "synthetic max_sessions proposal fixture",
    }


class NodeCapacityMaxSessionsProposalRendererTests(unittest.TestCase):
    def write_evidence(self, root: pathlib.Path) -> pathlib.Path:
        evidence = root / "evidence"
        evidence.mkdir()
        plan_path = evidence / "plan.json"
        plan_path.write_text(
            json.dumps(PLAN_RENDERER.build_plan(PROFILE), sort_keys=True),
            encoding="utf-8",
        )
        report_entries: list[dict[str, str]] = []
        for index, scenario_id in enumerate(SCENARIO_IDS, start=1):
            report_path = evidence / f"{scenario_id}.json"
            report_path.write_text(
                json.dumps(make_report(scenario_id, index), sort_keys=True),
                encoding="utf-8",
            )
            report_entries.append(
                {
                    "scenario_id": scenario_id,
                    "path": report_path.relative_to(root).as_posix(),
                }
            )
        manifest = {
            "schema_version": 1,
            "load_plan": plan_path.relative_to(root).as_posix(),
            "reports": report_entries,
        }
        manifest_path = evidence / "coverage.json"
        manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
        return manifest_path

    def render(
        self,
        manifest_path: pathlib.Path,
        root: pathlib.Path,
        *,
        candidate: object = 3,
        node_profile: object = NODE_PROFILE,
        software_revision: object = REVISION,
    ) -> dict[str, object]:
        return MODULE.render_proposal(
            manifest_path,
            candidate,
            expected_node_profile=node_profile,
            expected_software_revision=software_revision,
            repo_root=root,
        )

    def test_renders_deterministic_reviewable_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            manifest_path = self.write_evidence(root)
            proposal = self.render(manifest_path, root)

        self.assertEqual(
            proposal,
            {
                "schema_version": 1,
                "coverage_manifest": "evidence/coverage.json",
                "node_profile": NODE_PROFILE,
                "software_revision": REVISION,
                "candidate_max_sessions": 3,
                "measured_recommended_max_sessions": 3,
                "headroom_sessions": 0,
                "report_count": len(SCENARIO_IDS),
                "safety_margin_percent": 25,
            },
        )

    def test_more_conservative_candidate_preserves_headroom(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            manifest_path = self.write_evidence(root)
            proposal = self.render(manifest_path, root, candidate=2)

        self.assertEqual(proposal["candidate_max_sessions"], 2)
        self.assertEqual(proposal["measured_recommended_max_sessions"], 3)
        self.assertEqual(proposal["headroom_sessions"], 1)

    def test_unsupported_candidate_and_identity_fail_without_echoing_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            manifest_path = self.write_evidence(root)
            malicious_profile = "different\x1b[31m-profile"
            for candidate, profile, revision in (
                (4, NODE_PROFILE, REVISION),
                (1, malicious_profile, REVISION),
                (1, NODE_PROFILE, "f" * 40),
            ):
                with self.subTest(candidate=candidate, profile=profile, revision=revision):
                    with self.assertRaises(MODULE.CapacityProposalRenderError) as context:
                        self.render(
                            manifest_path,
                            root,
                            candidate=candidate,
                            node_profile=profile,
                            software_revision=revision,
                        )
                    self.assertEqual(
                        str(context.exception),
                        "coverage manifest or deployment proposal is invalid",
                    )
                    self.assertNotIn("\x1b", str(context.exception))
                    self.assertNotIn(malicious_profile, str(context.exception))

    def test_manifest_must_stay_inside_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as other:
            root = pathlib.Path(directory)
            outside = pathlib.Path(other) / "coverage.json"
            outside.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(
                MODULE.CapacityProposalRenderError,
                "repository file",
            ):
                self.render(outside, root)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_manifest_path_must_not_traverse_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            manifest_path = self.write_evidence(root)
            alias = root / "alias"
            try:
                alias.symlink_to(manifest_path.parent, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")
            with self.assertRaisesRegex(
                MODULE.CapacityProposalRenderError,
                "must not traverse symlinks",
            ):
                self.render(alias / manifest_path.name, root)

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
