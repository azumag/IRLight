from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import unittest
import uuid
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validate-node-capacity-coverage-manifest.py"
SPEC = importlib.util.spec_from_file_location("node_capacity_coverage_manifest", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

RENDERER_PATH = ROOT / "scripts" / "render-node-capacity-load-plan.py"
RENDERER_SPEC = importlib.util.spec_from_file_location(
    "node_capacity_coverage_manifest_test_renderer", RENDERER_PATH
)
assert RENDERER_SPEC is not None and RENDERER_SPEC.loader is not None
RENDERER = importlib.util.module_from_spec(RENDERER_SPEC)
sys.modules[RENDERER_SPEC.name] = RENDERER
RENDERER_SPEC.loader.exec_module(RENDERER)

PROFILE = "720p30/1080p30 mix qa-v1"
REVISION = "0123456789abcdef0123456789abcdef01234567"
SCENARIO_IDS = [scenario_id for scenario_id, _description in RENDERER.SCENARIOS]


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
        "notes": "synthetic manifest-validator fixture",
    }


class NodeCapacityCoverageManifestTests(unittest.TestCase):
    def write_evidence(self, root: pathlib.Path) -> tuple[pathlib.Path, dict[str, object]]:
        evidence = root / "evidence"
        evidence.mkdir()
        plan_path = evidence / "plan.json"
        plan_path.write_text(
            json.dumps(RENDERER.build_plan(PROFILE), sort_keys=True), encoding="utf-8"
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
        payload: dict[str, object] = {
            "schema_version": 1,
            "load_plan": plan_path.relative_to(root).as_posix(),
            "reports": report_entries,
        }
        manifest_path = evidence / "coverage.json"
        manifest_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        return manifest_path, payload

    def test_canonical_manifest_revalidates_complete_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            manifest_path, _payload = self.write_evidence(root)
            summary = MODULE.validate_manifest_file(manifest_path, repo_root=root)

        self.assertTrue(summary["valid"])
        self.assertEqual(summary["scenario_ids"], SCENARIO_IDS)
        self.assertEqual(summary["report_count"], len(SCENARIO_IDS))
        self.assertEqual(summary["software_revision"], REVISION)
        self.assertEqual(summary["recommended_max_sessions"], 3)

    def test_single_report_cannot_satisfy_complete_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            _manifest_path, payload = self.write_evidence(root)
            payload["reports"] = [payload["reports"][0]]
            with self.assertRaisesRegex(
                MODULE.CapacityCoverageManifestError, "scenario coverage is invalid"
            ):
                MODULE.validate_manifest(payload, repo_root=root)

    def test_unknown_field_and_duplicate_binding_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            _manifest_path, payload = self.write_evidence(root)
            payload["unexpected"] = True
            with self.assertRaisesRegex(
                MODULE.CapacityCoverageManifestError, "unexpected top-level shape"
            ):
                MODULE.validate_manifest(payload, repo_root=root)

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            _manifest_path, payload = self.write_evidence(root)
            payload["reports"].append(dict(payload["reports"][0]))
            with self.assertRaisesRegex(
                MODULE.CapacityCoverageManifestError, "duplicate report binding"
            ):
                MODULE.validate_manifest(payload, repo_root=root)

    def test_untrusted_scenario_id_is_not_echoed_in_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            _manifest_path, payload = self.write_evidence(root)
            malicious_id = "normal-input\x1b[31m"
            first = payload["reports"][0]
            second = payload["reports"][1]
            first["scenario_id"] = malicious_id
            second["scenario_id"] = malicious_id
            with self.assertRaises(MODULE.CapacityCoverageManifestError) as context:
                MODULE.validate_manifest(payload, repo_root=root)

        message = str(context.exception)
        self.assertIn("duplicate report binding", message)
        self.assertNotIn("\x1b", message)
        self.assertNotIn(malicious_id, message)

    def test_duplicate_json_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "coverage.json"
            path.write_text(
                '{"schema_version":1,"schema_version":1,"load_plan":"x","reports":[]}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                MODULE.CapacityCoverageManifestError, "duplicate JSON key"
            ):
                MODULE.load_manifest(path)

    def test_manifest_symlink_and_special_file_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            target = root / "target.json"
            target.write_text("{}\n", encoding="utf-8")
            link = root / "coverage.json"
            link.symlink_to(target)
            with self.assertRaisesRegex(
                MODULE.CapacityCoverageManifestError, "regular file"
            ):
                MODULE.load_manifest(link)

        if hasattr(os, "mkfifo"):
            with tempfile.TemporaryDirectory() as directory:
                fifo = pathlib.Path(directory) / "coverage.fifo"
                os.mkfifo(fifo)
                with self.assertRaisesRegex(
                    MODULE.CapacityCoverageManifestError, "regular file"
                ):
                    MODULE.load_manifest(fifo)

    def test_manifest_oversize_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "coverage.json"
            with path.open("wb") as handle:
                handle.truncate(MODULE.MAX_MANIFEST_BYTES + 1)
            with self.assertRaisesRegex(
                MODULE.CapacityCoverageManifestError, "size limit"
            ):
                MODULE.load_manifest(path)

    def test_manifest_path_replacement_during_read_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            original = root / "coverage.json"
            replacement = root / "replacement.json"
            original.write_text("{}\n", encoding="utf-8")
            replacement.write_text("{}\n", encoding="utf-8")
            original_stat = os.lstat(original)
            replacement_stat = os.lstat(replacement)
            self.assertNotEqual(
                (original_stat.st_dev, original_stat.st_ino),
                (replacement_stat.st_dev, replacement_stat.st_ino),
            )
            with mock.patch.object(
                MODULE.os, "lstat", side_effect=[original_stat, replacement_stat]
            ):
                with self.assertRaisesRegex(
                    MODULE.CapacityCoverageManifestError, "changed while reading"
                ):
                    MODULE.load_manifest(original)

    def test_referenced_symlink_and_path_escape_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            _manifest_path, payload = self.write_evidence(root)
            first = payload["reports"][0]
            original = root / first["path"]
            target = original.with_name("real-report.json")
            original.replace(target)
            original.symlink_to(target)
            with self.assertRaisesRegex(
                MODULE.CapacityCoverageManifestError, "must not traverse symlinks"
            ):
                MODULE.validate_manifest(payload, repo_root=root)

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            _manifest_path, payload = self.write_evidence(root)
            payload["load_plan"] = "../outside.json"
            with self.assertRaisesRegex(
                MODULE.CapacityCoverageManifestError, "inside the repository"
            ):
                MODULE.validate_manifest(payload, repo_root=root)

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
