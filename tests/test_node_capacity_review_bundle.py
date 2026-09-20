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


def load_module(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


BUNDLE_RENDERER_PATH = ROOT / "scripts" / "render-node-capacity-review-bundle.py"
BUNDLE_VALIDATOR_PATH = ROOT / "scripts" / "validate-node-capacity-review-bundle.py"
PROPOSAL_RENDERER_PATH = ROOT / "scripts" / "render-node-capacity-max-sessions-proposal.py"
PLAN_RENDERER_PATH = ROOT / "scripts" / "render-node-capacity-load-plan.py"

BUNDLE_RENDERER = load_module(BUNDLE_RENDERER_PATH, "node_capacity_review_bundle_renderer")
BUNDLE_VALIDATOR = load_module(BUNDLE_VALIDATOR_PATH, "node_capacity_review_bundle_validator")
PROPOSAL_RENDERER = load_module(PROPOSAL_RENDERER_PATH, "node_capacity_review_bundle_proposal_renderer")
PLAN_RENDERER = load_module(PLAN_RENDERER_PATH, "node_capacity_review_bundle_plan_renderer")

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
        "notes": "synthetic digest-pinned review bundle fixture",
    }


class NodeCapacityReviewBundleTests(unittest.TestCase):
    def write_evidence(self, root: pathlib.Path) -> pathlib.Path:
        evidence = root / "evidence"
        evidence.mkdir()
        plan_path = evidence / "plan.json"
        plan_path.write_text(
            json.dumps(PLAN_RENDERER.build_plan(PROFILE), sort_keys=True),
            encoding="utf-8",
        )
        reports: list[dict[str, str]] = []
        for index, scenario_id in enumerate(SCENARIO_IDS, start=1):
            report_path = evidence / f"{scenario_id}.json"
            report_path.write_text(
                json.dumps(make_report(scenario_id, index), sort_keys=True),
                encoding="utf-8",
            )
            reports.append(
                {
                    "scenario_id": scenario_id,
                    "path": report_path.relative_to(root).as_posix(),
                }
            )
        manifest_path = evidence / "coverage.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "load_plan": plan_path.relative_to(root).as_posix(),
                    "reports": reports,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return manifest_path

    def write_proposal(
        self, root: pathlib.Path
    ) -> tuple[pathlib.Path, pathlib.Path, dict[str, object]]:
        manifest_path = self.write_evidence(root)
        proposal = PROPOSAL_RENDERER.render_proposal(
            manifest_path,
            3,
            expected_node_profile=NODE_PROFILE,
            expected_software_revision=REVISION,
            repo_root=root,
        )
        proposal_path = root / "evidence" / "max-sessions-proposal.json"
        proposal_path.write_text(
            json.dumps(proposal, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return proposal_path, manifest_path, proposal

    def write_bundle(
        self, root: pathlib.Path
    ) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path, dict[str, object]]:
        proposal_path, manifest_path, _proposal = self.write_proposal(root)
        bundle = BUNDLE_RENDERER.render_bundle(
            proposal_path,
            expected_node_profile=NODE_PROFILE,
            expected_software_revision=REVISION,
            repo_root=root,
        )
        bundle_path = root / "evidence" / "review-bundle.json"
        bundle_path.write_text(
            json.dumps(bundle, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return bundle_path, proposal_path, manifest_path, bundle

    def validate(self, bundle_path: pathlib.Path, root: pathlib.Path, **overrides: object):
        return BUNDLE_VALIDATOR.validate_bundle_file(
            bundle_path,
            expected_node_profile=overrides.get("node_profile", NODE_PROFILE),
            expected_software_revision=overrides.get("software_revision", REVISION),
            repo_root=root,
        )

    def test_renders_and_validates_exact_digest_pinned_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            bundle_path, _proposal_path, _manifest_path, bundle = self.write_bundle(root)
            validated = self.validate(bundle_path, root)
            second = BUNDLE_RENDERER.render_bundle(
                root / str(bundle["proposal_path"]),
                expected_node_profile=NODE_PROFILE,
                expected_software_revision=REVISION,
                repo_root=root,
            )

        self.assertEqual(validated, bundle)
        self.assertEqual(second, bundle)
        self.assertEqual(bundle["candidate_max_sessions"], 3)
        self.assertEqual(bundle["measured_recommended_max_sessions"], 3)
        self.assertRegex(str(bundle["proposal_sha256"]), r"^[0-9a-f]{64}$")
        self.assertRegex(str(bundle["coverage_manifest_sha256"]), r"^[0-9a-f]{64}$")

    def test_semantically_equivalent_byte_changes_invalidate_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            bundle_path, proposal_path, manifest_path, _bundle = self.write_bundle(root)

            original_manifest = manifest_path.read_text(encoding="utf-8")
            manifest_path.write_text(original_manifest + "\n", encoding="utf-8")
            self.assertEqual(json.loads(original_manifest), json.loads(manifest_path.read_text()))
            with self.assertRaises(BUNDLE_VALIDATOR.CapacityReviewBundleValidationError):
                self.validate(bundle_path, root)

            manifest_path.write_text(original_manifest, encoding="utf-8")
            proposal_value = json.loads(proposal_path.read_text(encoding="utf-8"))
            proposal_path.write_text(
                json.dumps(proposal_value, sort_keys=True),
                encoding="utf-8",
            )
            with self.assertRaises(BUNDLE_VALIDATOR.CapacityReviewBundleValidationError):
                self.validate(bundle_path, root)

    def test_tampered_bundle_and_wrong_identity_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            bundle_path, _proposal_path, _manifest_path, bundle = self.write_bundle(root)

            tampered = dict(bundle)
            tampered["proposal_sha256"] = "0" * 64
            bundle_path.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaisesRegex(
                BUNDLE_VALIDATOR.CapacityReviewBundleValidationError,
                "does not match current pinned capacity evidence",
            ):
                self.validate(bundle_path, root)

            bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
            malicious_profile = "wrong\x1b[31m-profile"
            with self.assertRaises(
                BUNDLE_VALIDATOR.CapacityReviewBundleValidationError
            ) as context:
                self.validate(bundle_path, root, node_profile=malicious_profile)

        self.assertNotIn(malicious_profile, str(context.exception))
        self.assertNotIn("\x1b", str(context.exception))

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_bundle_path_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            bundle_path, _proposal_path, _manifest_path, _bundle = self.write_bundle(root)
            alias = root / "bundle-link.json"
            try:
                alias.symlink_to(bundle_path)
            except OSError as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")
            with self.assertRaisesRegex(
                BUNDLE_VALIDATOR.CapacityReviewBundleValidationError,
                "could not be read safely",
            ):
                self.validate(alias, root)

    def test_bundle_tools_do_not_import_network_or_load_execution_clients(self) -> None:
        for path in (BUNDLE_RENDERER_PATH, BUNDLE_VALIDATOR_PATH):
            source = path.read_text(encoding="utf-8")
            for forbidden in (
                "import subprocess",
                "from subprocess",
                "import socket",
                "from socket",
                "import requests",
                "import urllib",
            ):
                with self.subTest(path=path.name, forbidden=forbidden):
                    self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
