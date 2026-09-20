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


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


module = _load("release_acceptance_checklist", SCRIPT)
PLAN_RENDERER = _load(
    "release_acceptance_capacity_plan_renderer",
    ROOT / "scripts" / "render-node-capacity-load-plan.py",
)
PLANNED_ASSEMBLER = _load(
    "release_acceptance_capacity_planned_assembler",
    ROOT / "scripts" / "assemble-node-capacity-planned-report.py",
)

PROFILE_LABEL = "720p30/1080p30 mix release-test-v2"
NODE_PROFILE = "unit-test node profile"
SOFTWARE_REVISION = "0123456789abcdef0123456789abcdef01234567"
LOAD_LEVELS = [1, 2, 4, 8]


def _trial(sessions: int) -> dict[str, object]:
    outcome = "fail" if sessions == 8 else "pass"
    return {
        "concurrent_sessions": sessions,
        "duration_seconds": 300.0,
        "outcome": outcome,
        "cpu_peak_percent": float(sessions * 20),
        "memory_rss_peak_bytes": sessions * 100_000_000,
        "egress_peak_bps": float(sessions * 5_000_000),
        "failed_sessions": 1 if outcome == "fail" else 0,
        "unexpected_reconnects": 0,
    }


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

    def _write_capacity_coverage_evidence(
        self,
        *,
        provenance_bound: bool = False,
    ) -> tuple[Path, Path]:
        directory = Path(tempfile.mkdtemp(prefix=".release-capacity-coverage-", dir=ROOT))
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)

        plan_value = PLAN_RENDERER.build_plan(PROFILE_LABEL)
        plan_path = directory / "plan.json"
        plan_path.write_text(json.dumps(plan_value, sort_keys=True) + "\n", encoding="utf-8")

        bindings: list[dict[str, str]] = []
        first_report: Path | None = None
        for index, (scenario_id, _description) in enumerate(PLAN_RENDERER.SCENARIOS, start=1):
            records = [_trial(sessions) for sessions in LOAD_LEVELS]
            trials_path = directory / f"{scenario_id}.trials.jsonl"
            trials_path.write_text(
                "\n".join(
                    json.dumps(record, separators=(",", ":"), sort_keys=True)
                    for record in records
                )
                + "\n",
                encoding="utf-8",
            )
            run_manifest_value = {
                "schema_version": 1,
                "plan_sha256": PLANNED_ASSEMBLER._canonical_digest(plan_value),
                "profile_label": PROFILE_LABEL,
                "scenario_id": scenario_id,
                "node_profile": NODE_PROFILE,
                "software_revision": SOFTWARE_REVISION,
                "failure_policy": "continue",
                "planned_load_levels": LOAD_LEVELS,
                "tested_load_levels": LOAD_LEVELS,
                "boundary_found": True,
                "completed_plan": True,
                "trials_sha256": PLANNED_ASSEMBLER._canonical_digest(records),
            }
            run_manifest_path = directory / f"{scenario_id}.run.json"
            run_manifest_path.write_text(
                json.dumps(run_manifest_value, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            report_value = PLANNED_ASSEMBLER.assemble_planned_report(
                plan_path=plan_path,
                trials_path=trials_path,
                run_manifest_path=run_manifest_path,
                scenario_id=scenario_id,
                run_id=str(uuid.UUID(int=index)),
                safety_margin_percent=25,
                notes="synthetic release acceptance provenance fixture",
            )
            report_path = directory / f"{scenario_id}.report.json"
            report_path.write_text(
                json.dumps(report_value, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            if first_report is None:
                first_report = report_path

            entry = {
                "scenario_id": scenario_id,
                "path": report_path.relative_to(ROOT).as_posix(),
            }
            if provenance_bound:
                entry.update(
                    {
                        "trials_path": trials_path.relative_to(ROOT).as_posix(),
                        "run_manifest_path": run_manifest_path.relative_to(ROOT).as_posix(),
                    }
                )
            bindings.append(entry)

        manifest_path = directory / "coverage.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "schema_version": 2 if provenance_bound else 1,
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
            item for item in payload["items"] if item["id"] != "six-hour-soak"
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
        item = next(entry for entry in payload["items"] if entry["id"] == "obs-compatibility")
        item["status"] = "satisfied"
        with self.assertRaisesRegex(module.ChecklistValidationError, "require repository evidence"):
            module.validate_checklist(payload)

    def test_six_hour_soak_satisfied_rejects_arbitrary_repository_evidence(self) -> None:
        payload = self._canonical()
        item = next(entry for entry in payload["items"] if entry["id"] == "six-hour-soak")
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
        item = next(entry for entry in payload["items"] if entry["id"] == "six-hour-soak")
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
        item = next(entry for entry in payload["items"] if entry["id"] == "six-hour-soak")
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
        item = next(entry for entry in payload["items"] if entry["id"] == "six-hour-soak")
        item["status"] = "satisfied"
        item["evidence"] = [
            "docs/release-acceptance-checklist.md",
            report_path.relative_to(ROOT).as_posix(),
        ]
        module.validate_checklist(payload)

    def test_node_capacity_satisfied_rejects_arbitrary_repository_evidence(self) -> None:
        payload = self._canonical()
        item = next(entry for entry in payload["items"] if entry["id"] == "node-capacity-load")
        item["status"] = "satisfied"
        item["evidence"] = ["docs/node-capacity-load-evidence.md"]
        with self.assertRaisesRegex(
            module.ChecklistValidationError, "provenance-bound Node capacity coverage manifest"
        ):
            module.validate_checklist(payload)

    def test_node_capacity_satisfied_rejects_single_canonical_report(self) -> None:
        _manifest_path, report_path = self._write_capacity_coverage_evidence()
        payload = self._canonical()
        item = next(entry for entry in payload["items"] if entry["id"] == "node-capacity-load")
        item["status"] = "satisfied"
        item["evidence"] = [report_path.relative_to(ROOT).as_posix()]
        with self.assertRaisesRegex(
            module.ChecklistValidationError, "provenance-bound Node capacity coverage manifest"
        ):
            module.validate_checklist(payload)

    def test_node_capacity_satisfied_rejects_schema_v1_complete_coverage_manifest(self) -> None:
        manifest_path, _report_path = self._write_capacity_coverage_evidence()
        payload = self._canonical()
        item = next(entry for entry in payload["items"] if entry["id"] == "node-capacity-load")
        item["status"] = "satisfied"
        item["evidence"] = [manifest_path.relative_to(ROOT).as_posix()]
        with self.assertRaisesRegex(module.ChecklistValidationError, "bind raw run provenance"):
            module.validate_checklist(payload)

    def test_node_capacity_satisfied_accepts_provenance_bound_complete_coverage_manifest(self) -> None:
        manifest_path, _report_path = self._write_capacity_coverage_evidence(
            provenance_bound=True
        )
        payload = self._canonical()
        item = next(entry for entry in payload["items"] if entry["id"] == "node-capacity-load")
        item["status"] = "satisfied"
        item["evidence"] = [
            "docs/node-capacity-provenance-coverage.md",
            manifest_path.relative_to(ROOT).as_posix(),
        ]
        module.validate_checklist(payload)

    def test_missing_evidence_path_is_rejected(self) -> None:
        payload = self._canonical()
        item = next(entry for entry in payload["items"] if entry["id"] == "obs-compatibility")
        item["evidence"] = ["docs/does-not-exist-release-proof.json"]
        with self.assertRaisesRegex(module.ChecklistValidationError, "missing or unsafe"):
            module.validate_checklist(payload)

    def test_path_traversal_evidence_is_rejected(self) -> None:
        payload = self._canonical()
        item = next(entry for entry in payload["items"] if entry["id"] == "obs-compatibility")
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
                with self.assertRaisesRegex(module.ChecklistValidationError, "changed while reading"):
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
            with self.assertRaisesRegex(module.ChecklistValidationError, "changed while reading"):
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
