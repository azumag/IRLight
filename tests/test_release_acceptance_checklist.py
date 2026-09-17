from __future__ import annotations

import copy
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validate-release-acceptance-checklist.py"
CHECKLIST = ROOT / "docs" / "release-acceptance-checklist.json"

_spec = importlib.util.spec_from_file_location("release_acceptance_checklist", SCRIPT)
assert _spec is not None and _spec.loader is not None
module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(module)


class ReleaseAcceptanceChecklistTests(unittest.TestCase):
    def _canonical(self) -> dict[str, object]:
        return module.load_checklist(CHECKLIST)

    def _write_soak_report(
        self,
        *,
        target_duration_seconds: int,
        observed_duration_seconds: int,
        outcome: str = "pass",
    ) -> Path:
        fd, name = tempfile.mkstemp(
            prefix=".release-soak-test-",
            suffix=".json",
            dir=ROOT,
        )
        os.close(fd)
        report_path = Path(name)
        self.addCleanup(report_path.unlink, missing_ok=True)
        report_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": "7c4dff13-6795-45dc-b6cb-2a6667477b0c",
                    "scenario": "unit-test long-running acceptance evidence",
                    "target_duration_seconds": target_duration_seconds,
                    "outcome": outcome,
                    "samples": [
                        {
                            "elapsed_seconds": 0,
                            "memory_rss_bytes": 1000,
                            "cpu_percent": 10.0,
                            "open_fds": 5,
                            "processes": 2,
                            "zombies": 0,
                            "bitrate_bps": 1000.0,
                            "av_sync_drift_ms": 0.0,
                            "timestamp_errors": 0,
                            "unexpected_reconnects": 0,
                        },
                        {
                            "elapsed_seconds": observed_duration_seconds,
                            "memory_rss_bytes": 1100,
                            "cpu_percent": 20.0,
                            "open_fds": 5,
                            "processes": 2,
                            "zombies": 0,
                            "bitrate_bps": 1000.0,
                            "av_sync_drift_ms": 1.0,
                            "timestamp_errors": 0,
                            "unexpected_reconnects": 0,
                        },
                    ],
                    "cleanup": {
                        "verified": True,
                        "details": "unit-test cleanup verified",
                    },
                    "notes": "synthetic validator fixture only",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return report_path

    def test_repository_checklist_is_valid_and_not_ready(self) -> None:
        payload = self._canonical()
        module.validate_checklist(payload)
        self.assertFalse(payload["qa_acceptance_ready"])

    def test_duplicate_item_id_is_rejected(self) -> None:
        payload = self._canonical()
        duplicate = copy.deepcopy(payload["items"][0])
        payload["items"].append(duplicate)
        with self.assertRaisesRegex(module.ChecklistValidationError, "item ids"):
            module.validate_checklist(payload)

    def test_missing_required_item_is_rejected(self) -> None:
        payload = self._canonical()
        payload["items"] = [
            item
            for item in payload["items"]
            if item["id"] != "six-hour-soak"
        ]
        with self.assertRaisesRegex(module.ChecklistValidationError, "missing required"):
            module.validate_checklist(payload)

    def test_qa_acceptance_ready_cannot_overclaim_pending_items(self) -> None:
        payload = self._canonical()
        payload["qa_acceptance_ready"] = True
        with self.assertRaisesRegex(module.ChecklistValidationError, "qa_acceptance_ready"):
            module.validate_checklist(payload)

    def test_satisfied_item_requires_evidence(self) -> None:
        payload = self._canonical()
        item = next(
            entry for entry in payload["items"] if entry["id"] == "obs-compatibility"
        )
        item["status"] = "satisfied"
        with self.assertRaisesRegex(module.ChecklistValidationError, "require repository evidence"):
            module.validate_checklist(payload)

    def test_six_hour_soak_satisfied_rejects_arbitrary_repository_evidence(self) -> None:
        payload = self._canonical()
        item = next(
            entry for entry in payload["items"] if entry["id"] == "six-hour-soak"
        )
        item["status"] = "satisfied"
        item["evidence"] = ["docs/release-acceptance-checklist.md"]

        with self.assertRaisesRegex(
            module.ChecklistValidationError, "canonical passing six-hour-or-longer soak report"
        ):
            module.validate_checklist(payload)

    def test_six_hour_soak_satisfied_rejects_short_canonical_pass(self) -> None:
        report_path = self._write_soak_report(
            target_duration_seconds=600,
            observed_duration_seconds=600,
        )
        payload = self._canonical()
        item = next(
            entry for entry in payload["items"] if entry["id"] == "six-hour-soak"
        )
        item["status"] = "satisfied"
        item["evidence"] = [report_path.relative_to(ROOT).as_posix()]

        with self.assertRaisesRegex(
            module.ChecklistValidationError, "target_duration_seconds must be >= 21600"
        ):
            module.validate_checklist(payload)

    def test_six_hour_soak_satisfied_rejects_nonpassing_canonical_report(self) -> None:
        report_path = self._write_soak_report(
            target_duration_seconds=21600,
            observed_duration_seconds=21600,
            outcome="fail",
        )
        payload = self._canonical()
        item = next(
            entry for entry in payload["items"] if entry["id"] == "six-hour-soak"
        )
        item["status"] = "satisfied"
        item["evidence"] = [report_path.relative_to(ROOT).as_posix()]

        with self.assertRaisesRegex(module.ChecklistValidationError, "outcome must be pass"):
            module.validate_checklist(payload)

    def test_six_hour_soak_satisfied_accepts_canonical_six_hour_pass(self) -> None:
        report_path = self._write_soak_report(
            target_duration_seconds=21600,
            observed_duration_seconds=21600,
        )
        payload = self._canonical()
        item = next(
            entry for entry in payload["items"] if entry["id"] == "six-hour-soak"
        )
        item["status"] = "satisfied"
        item["evidence"] = [
            "docs/release-acceptance-checklist.md",
            report_path.relative_to(ROOT).as_posix(),
        ]

        module.validate_checklist(payload)

    def test_node_capacity_satisfied_rejects_arbitrary_repository_evidence(self) -> None:
        payload = self._canonical()
        item = next(
            entry for entry in payload["items"] if entry["id"] == "node-capacity-load"
        )
        item["status"] = "satisfied"
        item["evidence"] = ["docs/node-capacity-load-evidence.md"]

        with self.assertRaisesRegex(
            module.ChecklistValidationError, "canonical Node capacity report"
        ):
            module.validate_checklist(payload)

    def test_node_capacity_satisfied_accepts_valid_canonical_report(self) -> None:
        payload = self._canonical()
        item = next(
            entry for entry in payload["items"] if entry["id"] == "node-capacity-load"
        )
        fd, name = tempfile.mkstemp(
            prefix=".release-capacity-test-",
            suffix=".json",
            dir=ROOT,
        )
        os.close(fd)
        report_path = Path(name)
        self.addCleanup(report_path.unlink, missing_ok=True)
        report_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": "8f75d865-5e7c-4ffd-a5dc-e73fab5f39e1",
                    "node_profile": "unit-test node profile",
                    "software_revision": "0123456789abcdef0123456789abcdef01234567",
                    "scenario": "unit-test approved acceptance policy",
                    "safety_margin_percent": 25,
                    "trials": [
                        {
                            "concurrent_sessions": 2,
                            "duration_seconds": 120,
                            "outcome": "pass",
                            "cpu_peak_percent": 50.0,
                            "memory_rss_peak_bytes": 1000,
                            "egress_peak_bps": 1000.0,
                            "failed_sessions": 0,
                            "unexpected_reconnects": 0,
                        },
                        {
                            "concurrent_sessions": 4,
                            "duration_seconds": 120,
                            "outcome": "fail",
                            "cpu_peak_percent": 90.0,
                            "memory_rss_peak_bytes": 2000,
                            "egress_peak_bps": 2000.0,
                            "failed_sessions": 1,
                            "unexpected_reconnects": 0,
                        },
                    ],
                    "notes": "unit-test evidence",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        item["status"] = "satisfied"
        item["evidence"] = [
            "docs/node-capacity-load-evidence.md",
            report_path.relative_to(ROOT).as_posix(),
        ]

        module.validate_checklist(payload)

    def test_missing_evidence_path_is_rejected(self) -> None:
        payload = self._canonical()
        item = next(
            entry for entry in payload["items"] if entry["id"] == "obs-compatibility"
        )
        item["evidence"] = ["docs/does-not-exist-release-proof.json"]
        with self.assertRaisesRegex(module.ChecklistValidationError, "missing or unsafe"):
            module.validate_checklist(payload)

    def test_path_traversal_evidence_is_rejected(self) -> None:
        payload = self._canonical()
        item = next(
            entry for entry in payload["items"] if entry["id"] == "obs-compatibility"
        )
        item["evidence"] = ["../outside-proof.json"]
        with self.assertRaisesRegex(module.ChecklistValidationError, "inside the repository"):
            module.validate_checklist(payload)

    def test_unknown_status_is_rejected(self) -> None:
        payload = self._canonical()
        payload["items"][0]["status"] = "green"
        with self.assertRaisesRegex(module.ChecklistValidationError, "unsupported status"):
            module.validate_checklist(payload)

    def test_non_string_status_is_rejected_without_traceback(self) -> None:
        payload = self._canonical()
        payload["items"][0]["status"] = ["satisfied"]
        with self.assertRaisesRegex(module.ChecklistValidationError, "unsupported status"):
            module.validate_checklist(payload)

    def test_float_schema_version_is_rejected(self) -> None:
        payload = self._canonical()
        payload["schema_version"] = 1.0
        with self.assertRaisesRegex(module.ChecklistValidationError, "schema_version"):
            module.validate_checklist(payload)

    def test_duplicate_json_key_is_rejected(self) -> None:
        fd, name = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        path = Path(name)
        self.addCleanup(path.unlink, missing_ok=True)
        path.write_text(
            '{"schema_version":1,"schema_version":1,"qa_acceptance_ready":false,'
            '"policy":"x","items":[]}',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(module.ChecklistValidationError, "duplicate JSON key"):
            module.load_checklist(path)


if __name__ == "__main__":
    unittest.main()
