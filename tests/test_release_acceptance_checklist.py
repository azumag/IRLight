from __future__ import annotations

import copy
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

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

    def _write_capacity_coverage_evidence(self) -> tuple[Path, Path]:
        directory = Path(tempfile.mkdtemp(prefix=".release-capacity-coverage-", dir=ROOT))
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)

        renderer_path = ROOT / "scripts" / "render-node-capacity-load-plan.py"
        renderer_spec = importlib.util.spec_from_file_location(
            f"release_capacity_renderer_{uuid.uuid4().hex}", renderer_path
        )
        assert renderer_spec is not None and renderer_spec.loader is not None
        renderer = importlib.util.module_from_spec(renderer_spec)
        sys.modules[renderer_spec.name] = renderer
        renderer_spec.loader.exec_module(renderer)

        profile = "720p30/1080p30 mix release-test-v1"
        revision = "0123456789abcdef0123456789abcdef01234567"
        plan_path = directory / "plan.json"
        plan_path.write_text(
            json.dumps(renderer.build_plan(profile), sort_keys=True) + "\n",
            encoding="utf-8",
        )

        bindings: list[dict[str, str]] = []
        first_report: Path | None = None
        for index, (scenario_id, _description) in enumerate(renderer.SCENARIOS, start=1):
            report_path = directory / f"{scenario_id}.json"
            if first_report is None:
                first_report = report_path
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
            report_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "run_id": str(uuid.UUID(int=index)),
                        "node_profile": "unit-test node profile",
                        "software_revision": revision,
                        "scenario": (
                            f"profile={profile}; scenario={scenario_id}; approved acceptance policy"
                        ),
                        "safety_margin_percent": 25,
                        "trials": trials,
                        "notes": "synthetic validator fixture only",
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            bindings.append(
                {
                    "scenario_id": scenario_id,
                    "path": report_path.relative_to(ROOT).as_posix(),
                }
            )

        manifest_path = directory / "coverage.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "load_plan": plan_path.relative_to(ROOT).as_posix(),
                    "reports": bindings,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        assert first_report is not None
        return manifest_path, first_report

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
            module.ChecklistValidationError, "canonical Node capacity coverage manifest"
        ):
            module.validate_checklist(payload)

    def test_node_capacity_satisfied_rejects_single_canonical_report(self) -> None:
        _manifest_path, report_path = self._write_capacity_coverage_evidence()
        payload = self._canonical()
        item = next(
            entry for entry in payload["items"] if entry["id"] == "node-capacity-load"
        )
        item["status"] = "satisfied"
        item["evidence"] = [report_path.relative_to(ROOT).as_posix()]

        with self.assertRaisesRegex(
            module.ChecklistValidationError, "canonical Node capacity coverage manifest"
        ):
            module.validate_checklist(payload)

    def test_node_capacity_satisfied_accepts_complete_coverage_manifest(self) -> None:
        manifest_path, _report_path = self._write_capacity_coverage_evidence()
        payload = self._canonical()
        item = next(
            entry for entry in payload["items"] if entry["id"] == "node-capacity-load"
        )
        item["status"] = "satisfied"
        item["evidence"] = [
            "docs/node-capacity-load-evidence.md",
            manifest_path.relative_to(ROOT).as_posix(),
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

    def test_checklist_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.json"
            target.write_text("{}\n", encoding="utf-8")
            link = root / "checklist.json"
            link.symlink_to(target)
            with self.assertRaisesRegex(module.ChecklistValidationError, "regular file"):
                module.load_checklist(link)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO support is unavailable")
    def test_checklist_fifo_is_rejected_without_opening(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fifo = Path(directory) / "checklist.fifo"
            os.mkfifo(fifo)
            with self.assertRaisesRegex(module.ChecklistValidationError, "regular file"):
                module.load_checklist(fifo)

    def test_checklist_oversize_regular_file_is_rejected(self) -> None:
        fd, name = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        path = Path(name)
        self.addCleanup(path.unlink, missing_ok=True)
        with path.open("wb") as handle:
            handle.truncate(module.MAX_CHECKLIST_BYTES + 1)
        with self.assertRaisesRegex(module.ChecklistValidationError, "size limit"):
            module.load_checklist(path)

    def test_checklist_path_replacement_during_read_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "checklist.json"
            replacement = root / "replacement.json"
            original.write_text(CHECKLIST.read_text(encoding="utf-8"), encoding="utf-8")
            replacement.write_text(CHECKLIST.read_text(encoding="utf-8"), encoding="utf-8")
            original_stat = os.lstat(original)
            replacement_stat = os.lstat(replacement)
            self.assertNotEqual(
                (original_stat.st_dev, original_stat.st_ino),
                (replacement_stat.st_dev, replacement_stat.st_ino),
            )
            with mock.patch.object(
                module.os,
                "lstat",
                side_effect=[original_stat, replacement_stat],
            ):
                with self.assertRaisesRegex(
                    module.ChecklistValidationError, "changed while reading"
                ):
                    module.load_checklist(original)

    def test_checklist_in_place_change_during_read_is_rejected(self) -> None:
        fd, name = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        path = Path(name)
        self.addCleanup(path.unlink, missing_ok=True)
        path.write_text(CHECKLIST.read_text(encoding="utf-8"), encoding="utf-8")
        stable = path.stat()
        mutated = mock.Mock(
            st_mode=stable.st_mode,
            st_dev=stable.st_dev,
            st_ino=stable.st_ino,
            st_size=stable.st_size,
            st_mtime_ns=stable.st_mtime_ns + 1,
            st_ctime_ns=stable.st_ctime_ns + 1,
        )
        with mock.patch.object(
            module.os,
            "fstat",
            side_effect=[stable, stable, mutated],
        ):
            with self.assertRaisesRegex(
                module.ChecklistValidationError, "changed while reading"
            ):
                module.load_checklist(path)

    def test_invalid_utf8_checklist_is_reported_without_traceback(self) -> None:
        fd, name = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        path = Path(name)
        self.addCleanup(path.unlink, missing_ok=True)
        path.write_bytes(b"\xff\xfe\x00")
        with self.assertRaisesRegex(module.ChecklistValidationError, "could not be read"):
            module.load_checklist(path)


if __name__ == "__main__":
    unittest.main()
