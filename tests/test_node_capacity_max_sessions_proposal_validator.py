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
SCRIPT = ROOT / "scripts" / "validate-node-capacity-max-sessions-proposal.py"
SPEC = importlib.util.spec_from_file_location(
    "node_capacity_max_sessions_proposal_validator", SCRIPT
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

RENDERER_PATH = ROOT / "scripts" / "render-node-capacity-max-sessions-proposal.py"
RENDERER_SPEC = importlib.util.spec_from_file_location(
    "node_capacity_max_sessions_proposal_validator_test_renderer", RENDERER_PATH
)
assert RENDERER_SPEC is not None and RENDERER_SPEC.loader is not None
RENDERER = importlib.util.module_from_spec(RENDERER_SPEC)
sys.modules[RENDERER_SPEC.name] = RENDERER
RENDERER_SPEC.loader.exec_module(RENDERER)

PLAN_RENDERER_PATH = ROOT / "scripts" / "render-node-capacity-load-plan.py"
PLAN_RENDERER_SPEC = importlib.util.spec_from_file_location(
    "node_capacity_proposal_validator_test_plan_renderer", PLAN_RENDERER_PATH
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
        "notes": "synthetic persisted proposal fixture",
    }


class NodeCapacityMaxSessionsProposalValidatorTests(unittest.TestCase):
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

    def write_proposal(
        self,
        root: pathlib.Path,
        *,
        candidate: int = 3,
    ) -> tuple[pathlib.Path, dict[str, object]]:
        manifest_path = self.write_evidence(root)
        proposal = RENDERER.render_proposal(
            manifest_path,
            candidate,
            expected_node_profile=NODE_PROFILE,
            expected_software_revision=REVISION,
            repo_root=root,
        )
        proposal_path = root / "evidence" / "max-sessions-proposal.json"
        proposal_path.write_text(
            json.dumps(proposal, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return proposal_path, proposal

    def validate(
        self,
        proposal_path: pathlib.Path,
        root: pathlib.Path,
        *,
        node_profile: object = NODE_PROFILE,
        software_revision: object = REVISION,
    ) -> dict[str, object]:
        return MODULE.validate_proposal_file(
            proposal_path,
            expected_node_profile=node_profile,
            expected_software_revision=software_revision,
            repo_root=root,
        )

    def test_accepts_exact_canonical_persisted_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            proposal_path, proposal = self.write_proposal(root)
            validated = self.validate(proposal_path, root)

        self.assertEqual(validated, proposal)
        self.assertEqual(validated["candidate_max_sessions"], 3)
        self.assertEqual(validated["measured_recommended_max_sessions"], 3)

    def test_rejects_tampered_derived_values_and_bool_integer_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            proposal_path, proposal = self.write_proposal(root)
            for field, value in (
                ("measured_recommended_max_sessions", 2),
                ("headroom_sessions", 1),
                ("report_count", True),
                ("safety_margin_percent", True),
            ):
                with self.subTest(field=field):
                    tampered = dict(proposal)
                    tampered[field] = value
                    proposal_path.write_text(json.dumps(tampered), encoding="utf-8")
                    with self.assertRaisesRegex(
                        MODULE.CapacityProposalValidationError,
                        "does not match current canonical measured evidence",
                    ):
                        self.validate(proposal_path, root)

    def test_candidate_bool_and_unexpected_fields_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            proposal_path, proposal = self.write_proposal(root)

            bool_candidate = dict(proposal)
            bool_candidate["candidate_max_sessions"] = True
            proposal_path.write_text(json.dumps(bool_candidate), encoding="utf-8")
            with self.assertRaisesRegex(
                MODULE.CapacityProposalValidationError,
                "integer field is invalid",
            ):
                self.validate(proposal_path, root)

            unexpected = dict(proposal)
            unexpected["approved"] = True
            proposal_path.write_text(json.dumps(unexpected), encoding="utf-8")
            with self.assertRaisesRegex(
                MODULE.CapacityProposalValidationError,
                "unexpected top-level shape",
            ):
                self.validate(proposal_path, root)

    def test_duplicate_keys_and_invalid_utf8_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            proposal_path, _proposal = self.write_proposal(root)

            proposal_path.write_text(
                '{"schema_version":1,"schema_version":1}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                MODULE.CapacityProposalValidationError,
                "duplicate JSON key",
            ):
                self.validate(proposal_path, root)

            proposal_path.write_bytes(b"{\xff}")
            with self.assertRaisesRegex(
                MODULE.CapacityProposalValidationError,
                "valid UTF-8",
            ):
                self.validate(proposal_path, root)

    def test_wrong_deployment_identity_is_not_reflected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            proposal_path, _proposal = self.write_proposal(root)
            malicious_profile = "other\x1b[31m-profile"
            with self.assertRaises(MODULE.CapacityProposalValidationError) as context:
                self.validate(
                    proposal_path,
                    root,
                    node_profile=malicious_profile,
                )

        self.assertEqual(
            str(context.exception),
            "proposal evidence or deployment identity is invalid",
        )
        self.assertNotIn(malicious_profile, str(context.exception))
        self.assertNotIn("\x1b", str(context.exception))

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_proposal_path_must_not_traverse_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            proposal_path, _proposal = self.write_proposal(root)
            alias = root / "proposal-link.json"
            try:
                alias.symlink_to(proposal_path)
            except OSError as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")
            with self.assertRaisesRegex(
                MODULE.CapacityProposalValidationError,
                "must not traverse symlinks",
            ):
                self.validate(alias, root)

    def test_oversized_proposal_is_rejected_before_json_decode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            proposal_path, _proposal = self.write_proposal(root)
            proposal_path.write_bytes(b" " * (MODULE.MAX_PROPOSAL_BYTES + 1))
            with self.assertRaisesRegex(
                MODULE.CapacityProposalValidationError,
                "exceeds size limit",
            ):
                self.validate(proposal_path, root)

    def test_validator_has_no_load_execution_or_network_imports(self) -> None:
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
