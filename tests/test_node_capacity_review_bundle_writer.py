from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
WRITER_PATH = ROOT / "scripts" / "write-node-capacity-review-bundle.py"


def load_module(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


WRITER = load_module(WRITER_PATH, "node_capacity_review_bundle_writer")


def bundle_fixture() -> dict[str, object]:
    return {
        "schema_version": 1,
        "proposal_path": "evidence/proposal.json",
        "proposal_sha256": "1" * 64,
        "coverage_manifest": "evidence/coverage.json",
        "coverage_manifest_sha256": "2" * 64,
        "load_plan": {"path": "evidence/plan.json", "sha256": "3" * 64},
        "reports": [
            {
                "scenario_id": "normal-input",
                "path": "evidence/report.json",
                "sha256": "4" * 64,
            }
        ],
        "node_profile": "linux-x86_64 4 vCPU 8 GiB",
        "software_revision": "0123456789abcdef0123456789abcdef01234567",
        "candidate_max_sessions": 3,
        "measured_recommended_max_sessions": 3,
    }


class NodeCapacityReviewBundleWriterTests(unittest.TestCase):
    def test_writes_complete_bundle_to_repository_relative_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "evidence").mkdir()
            output = WRITER.write_bundle_atomically(
                bundle_fixture(),
                pathlib.Path("evidence/review-bundle.json"),
                repo_root=root,
            )

            self.assertEqual(output, root / "evidence" / "review-bundle.json")
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8")),
                bundle_fixture(),
            )
            self.assertTrue(output.read_text(encoding="utf-8").endswith("\n"))
            self.assertEqual(list((root / "evidence").glob("*.tmp")), [])

    def test_replace_failure_preserves_known_good_bundle_and_cleans_temp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            evidence = root / "evidence"
            evidence.mkdir()
            output = evidence / "review-bundle.json"
            output.write_text("known-good\n", encoding="utf-8")

            with mock.patch.object(WRITER.os, "replace", side_effect=OSError("injected")):
                with self.assertRaisesRegex(
                    WRITER.CapacityReviewBundleWriteError,
                    "could not be persisted atomically",
                ):
                    WRITER.write_bundle_atomically(
                        bundle_fixture(),
                        pathlib.Path("evidence/review-bundle.json"),
                        repo_root=root,
                    )

            self.assertEqual(output.read_text(encoding="utf-8"), "known-good\n")
            self.assertEqual(list(evidence.glob("*.tmp")), [])

    def test_refuses_to_replace_any_pinned_evidence_input(self) -> None:
        for path in (
            "evidence/proposal.json",
            "evidence/coverage.json",
            "evidence/plan.json",
            "evidence/report.json",
        ):
            with self.subTest(path=path), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                (root / "evidence").mkdir()
                with self.assertRaisesRegex(
                    WRITER.CapacityReviewBundleWriteError,
                    "must not replace pinned capacity evidence",
                ):
                    WRITER.write_bundle_atomically(
                        bundle_fixture(), pathlib.Path(path), repo_root=root
                    )

    def test_rejects_parent_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            with self.assertRaisesRegex(
                WRITER.CapacityReviewBundleWriteError,
                "repository-relative file path",
            ):
                WRITER.write_bundle_atomically(
                    bundle_fixture(),
                    pathlib.Path("evidence/../review-bundle.json"),
                    repo_root=root,
                )

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_existing_output_symlink_is_rejected_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            evidence = root / "evidence"
            evidence.mkdir()
            outside = root / "outside.json"
            outside.write_text("outside\n", encoding="utf-8")
            output = evidence / "review-bundle.json"
            try:
                output.symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")

            with self.assertRaisesRegex(
                WRITER.CapacityReviewBundleWriteError,
                "must be a regular file",
            ):
                WRITER.write_bundle_atomically(
                    bundle_fixture(),
                    pathlib.Path("evidence/review-bundle.json"),
                    repo_root=root,
                )

            self.assertEqual(outside.read_text(encoding="utf-8"), "outside\n")


if __name__ == "__main__":
    unittest.main()
