from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validate-node-capacity-max-sessions.py"
SPEC = importlib.util.spec_from_file_location("node_capacity_max_sessions", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

RENDERER_PATH = ROOT / "scripts" / "render-node-capacity-load-plan.py"
RENDERER_SPEC = importlib.util.spec_from_file_location(
    "node_capacity_max_sessions_test_renderer", RENDERER_PATH
)
assert RENDERER_SPEC is not None and RENDERER_SPEC.loader is not None
RENDERER = importlib.util.module_from_spec(RENDERER_SPEC)
sys.modules[RENDERER_SPEC.name] = RENDERER
RENDERER_SPEC.loader.exec_module(RENDERER)

PROFILE = "720p30/1080p30 mix qa-v1"
NODE_PROFILE = "linux-x86_64 4 vCPU 8 GiB"
REVISION = "0123456789abcdef0123456789abcdef01234567"
SCENARIO_IDS = [scenario_id for scenario_id, _description in RENDERER.SCENARIOS]


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
        "notes": "synthetic max_sessions-validator fixture",
    }


class NodeCapacityMaxSessionsTests(unittest.TestCase):
    def write_evidence(self, root: pathlib.Path) -> pathlib.Path:
        evidence = root / "evidence"
        evidence.mkdir()
        plan_path = evidence / "plan.json"
        plan_path.write_text(
            json.dumps(RENDERER.build_plan(PROFILE), sort_keys=True),
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

    def validate(
        self,
        manifest_path: pathlib.Path,
        candidate: object,
        root: pathlib.Path,
        *,
        node_profile: object = NODE_PROFILE,
        software_revision: object = REVISION,
    ) -> dict[str, object]:
        return MODULE.validate_max_sessions(
            manifest_path,
            candidate,
            expected_node_profile=node_profile,
            expected_software_revision=software_revision,
            repo_root=root,
        )

    def test_candidate_equal_to_measured_recommendation_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            manifest_path = self.write_evidence(root)
            summary = self.validate(manifest_path, 3, root)

        self.assertTrue(summary["valid"])
        self.assertEqual(summary["candidate_max_sessions"], 3)
        self.assertEqual(summary["recommended_max_sessions"], 3)
        self.assertEqual(summary["headroom_sessions"], 0)
        self.assertEqual(summary["report_count"], len(SCENARIO_IDS))
        self.assertEqual(summary["software_revision"], REVISION)
        self.assertEqual(summary["node_profile"], NODE_PROFILE)

    def test_more_conservative_candidate_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            manifest_path = self.write_evidence(root)
            summary = self.validate(manifest_path, 2, root)

        self.assertEqual(summary["candidate_max_sessions"], 2)
        self.assertEqual(summary["recommended_max_sessions"], 3)
        self.assertEqual(summary["headroom_sessions"], 1)

    def test_candidate_above_measured_recommendation_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            manifest_path = self.write_evidence(root)
            with self.assertRaisesRegex(
                MODULE.CapacityMaxSessionsError,
                "exceeds the measured recommendation",
            ):
                self.validate(manifest_path, 4, root)

    def test_deployment_profile_and_revision_must_match_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            manifest_path = self.write_evidence(root)
            with self.assertRaisesRegex(
                MODULE.CapacityMaxSessionsError,
                "node_profile does not match",
            ):
                self.validate(
                    manifest_path,
                    1,
                    root,
                    node_profile="different-node-profile",
                )
            with self.assertRaisesRegex(
                MODULE.CapacityMaxSessionsError,
                "software_revision does not match",
            ):
                self.validate(
                    manifest_path,
                    1,
                    root,
                    software_revision="f" * 40,
                )

    def test_invalid_expected_revision_fails_before_evidence_is_used(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            manifest_path = self.write_evidence(root)
            for revision in ("short", "A" * 40, True):
                with self.subTest(revision=revision):
                    with self.assertRaisesRegex(
                        MODULE.CapacityMaxSessionsError,
                        "lowercase 40-character",
                    ):
                        self.validate(
                            manifest_path,
                            1,
                            root,
                            software_revision=revision,
                        )

    def test_non_positive_and_boolean_candidates_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            manifest_path = self.write_evidence(root)
            for candidate in (0, -1, True):
                with self.subTest(candidate=candidate):
                    with self.assertRaisesRegex(
                        MODULE.CapacityMaxSessionsError,
                        "positive integer",
                    ):
                        self.validate(manifest_path, candidate, root)

    def test_incomplete_coverage_is_rejected_without_echoing_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            manifest_path = self.write_evidence(root)
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            malicious_id = "normal-input\x1b[31m"
            payload["reports"][0]["scenario_id"] = malicious_id
            manifest_path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaises(MODULE.CapacityMaxSessionsError) as context:
                self.validate(manifest_path, 1, root)

        self.assertEqual(str(context.exception), "coverage manifest is invalid")
        self.assertNotIn("\x1b", str(context.exception))
        self.assertNotIn(malicious_id, str(context.exception))

    def test_guardrail_has_no_load_execution_or_network_imports(self) -> None:
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
