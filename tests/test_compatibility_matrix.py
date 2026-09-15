from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = ROOT / "docs" / "compatibility-matrix.json"
VALIDATOR = ROOT / "scripts" / "validate-compatibility-matrix.py"
GUIDE = (ROOT / "docs" / "compatibility-testing.md").read_text(encoding="utf-8")


class CompatibilityMatrixTest(unittest.TestCase):
    def _run_validator(self, matrix: dict | None = None) -> subprocess.CompletedProcess[str]:
        if matrix is None:
            path = MATRIX_PATH
            return subprocess.run(
                [sys.executable, str(VALIDATOR), str(path)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "matrix.json"
            path.write_text(json.dumps(matrix), encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(VALIDATOR), str(path)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

    def test_canonical_matrix_passes_validator(self) -> None:
        result = self._run_validator()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("compatibility matrix valid:", result.stdout)

    def test_required_devices_and_destinations_remain_explicit(self) -> None:
        matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
        required = set(matrix["required_coverage"])
        self.assertEqual(
            required,
            {
                "pc.ffmpeg",
                "pc.obs",
                "pc.gstreamer",
                "mobile.ios_a",
                "mobile.ios_b",
                "mobile.android_a",
                "mobile.android_b",
                "hardware.rtmp_encoder",
                "destination.twitch",
                "destination.youtube",
                "destination.kick",
                "destination.custom_rtmp",
            },
        )

        by_coverage: dict[str, list[dict]] = {}
        for entry in matrix["entries"]:
            by_coverage.setdefault(entry["coverage"], []).append(entry)

        for coverage in required:
            with self.subTest(coverage=coverage):
                self.assertIn(coverage, by_coverage)

        for coverage in (
            "pc.obs",
            "pc.gstreamer",
            "mobile.ios_a",
            "mobile.ios_b",
            "mobile.android_a",
            "mobile.android_b",
            "hardware.rtmp_encoder",
            "destination.twitch",
            "destination.youtube",
            "destination.kick",
        ):
            with self.subTest(coverage=coverage):
                self.assertTrue(
                    all(item["status"] == "not_tested" for item in by_coverage[coverage])
                )

    def test_verified_entries_reference_existing_repository_evidence(self) -> None:
        matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
        verified = [
            entry
            for entry in matrix["entries"]
            if entry["status"] in {"automated", "manual_verified"}
        ]
        self.assertTrue(verified)
        for entry in verified:
            self.assertTrue(entry["evidence"], entry["id"])
            for evidence in entry["evidence"]:
                with self.subTest(entry=entry["id"], evidence=evidence):
                    self.assertTrue((ROOT / evidence).is_file())

    def test_validator_rejects_evidence_on_not_tested_claim(self) -> None:
        matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
        candidate = next(item for item in matrix["entries"] if item["status"] == "not_tested")
        candidate["evidence"] = ["README.md"]
        result = self._run_validator(matrix)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not_tested entries must not carry evidence", result.stderr)

    def test_validator_rejects_missing_required_coverage(self) -> None:
        matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
        matrix["entries"] = [
            item for item in matrix["entries"] if item["coverage"] != "mobile.ios_b"
        ]
        result = self._run_validator(matrix)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("entries do not cover required_coverage: mobile.ios_b", result.stderr)

    def test_manual_workflow_forbids_credentials_and_support_inference(self) -> None:
        for expected in (
            "not_tested",
            "Real stream keys",
            "Never promote FFmpeg or local MediaMTX results",
            "docs/compatibility-reports/",
            "PASS | WARN | FAIL",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, GUIDE)


if __name__ == "__main__":
    unittest.main()
