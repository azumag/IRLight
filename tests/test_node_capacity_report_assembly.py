from __future__ import annotations

import importlib.util
import json
import os
import stat
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "assemble_node_capacity_report",
    ROOT / "scripts" / "assemble-node-capacity-report.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
CapacityAssemblyError = MODULE.CapacityAssemblyError


def trial(
    sessions: int,
    outcome: str,
    *,
    failed_sessions: int = 0,
    reconnects: int = 0,
) -> dict:
    return {
        "concurrent_sessions": sessions,
        "duration_seconds": 120,
        "outcome": outcome,
        "cpu_peak_percent": float(sessions * 30),
        "memory_rss_peak_bytes": sessions * 100_000_000,
        "egress_peak_bps": sessions * 5_000_000,
        "failed_sessions": failed_sessions,
        "unexpected_reconnects": reconnects,
    }


class CapacityReportAssemblyTest(unittest.TestCase):
    def test_assembles_trials_and_reuses_canonical_validator(self) -> None:
        report = MODULE.assemble_report(
            trials=[
                trial(1, "pass"),
                trial(4, "pass"),
                trial(8, "fail", failed_sessions=1),
            ],
            run_id=str(uuid.uuid4()),
            node_profile="linux-x86_64 4 vCPU 8 GiB",
            software_revision="a" * 40,
            scenario="steady pass-through under an explicit acceptance policy",
            safety_margin_percent=25,
            notes="fixture",
        )
        self.assertEqual(
            [item["concurrent_sessions"] for item in report["trials"]], [1, 4, 8]
        )

    def test_rejects_duplicate_keys_nonfinite_constants_and_blank_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trials.jsonl"
            for raw, pattern in (
                ('{"concurrent_sessions":1,"concurrent_sessions":2}\n', "duplicate JSON key"),
                ('{"cpu_peak_percent":NaN}\n', "non-standard JSON numeric constant"),
                ('{}\n\n{}\n', "must not be blank"),
            ):
                path.write_text(raw, encoding="utf-8")
                with self.subTest(raw=raw):
                    with self.assertRaisesRegex(CapacityAssemblyError, pattern):
                        MODULE.load_trials_jsonl(path)

    def test_rejects_non_object_trial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trials.jsonl"
            path.write_text("[]\n", encoding="utf-8")
            with self.assertRaisesRegex(CapacityAssemblyError, "must contain a JSON object"):
                MODULE.load_trials_jsonl(path)

    def test_rejects_oversized_raw_evidence_before_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trials.jsonl"
            path.write_bytes(b"x" * (MODULE.MAX_TRIALS_JSONL_BYTES + 1))
            with self.assertRaisesRegex(CapacityAssemblyError, "maximum size"):
                MODULE.load_trials_jsonl(path)

    def test_rejects_excessive_trial_count(self) -> None:
        raw = "{}\n" * (MODULE.MAX_TRIAL_COUNT + 1)
        with self.assertRaisesRegex(CapacityAssemblyError, "at most"):
            MODULE.parse_trials_jsonl(raw)

    def test_snapshot_uses_recorder_sidecar_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trials.jsonl"
            path.write_text(json.dumps(trial(1, "pass")) + "\n", encoding="utf-8")
            with mock.patch.object(
                MODULE.fcntl,
                "flock",
                wraps=MODULE.fcntl.flock,
            ) as flock:
                loaded = MODULE.load_trials_snapshot(path)
            self.assertEqual(len(loaded), 1)
            self.assertTrue(path.with_name("trials.jsonl.lock").exists())
            self.assertTrue(
                any(call.args[1] == MODULE.fcntl.LOCK_SH for call in flock.call_args_list)
            )

    def test_snapshot_rejects_symbolic_link_trials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "real-trials.jsonl"
            alias = root / "trials.jsonl"
            target.write_text(json.dumps(trial(1, "pass")) + "\n", encoding="utf-8")
            alias.symlink_to(target)

            with self.assertRaisesRegex(CapacityAssemblyError, "regular file"):
                MODULE.load_trials_snapshot(alias)

            self.assertEqual(
                target.read_text(encoding="utf-8"),
                json.dumps(trial(1, "pass")) + "\n",
            )

    def test_snapshot_rejects_non_regular_trials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trials.jsonl"
            path.mkdir()
            with self.assertRaisesRegex(CapacityAssemblyError, "regular file"):
                MODULE.load_trials_snapshot(path)

    def test_snapshot_rejects_path_replacement_during_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "trials.jsonl"
            replacement = root / "replacement.jsonl"
            path.write_text(json.dumps(trial(1, "pass")) + "\n", encoding="utf-8")
            replacement.write_text(
                json.dumps(trial(2, "pass")) + "\n",
                encoding="utf-8",
            )
            original_stat = os.lstat(path)
            replacement_stat = os.lstat(replacement)
            self.assertNotEqual(
                (original_stat.st_dev, original_stat.st_ino),
                (replacement_stat.st_dev, replacement_stat.st_ino),
            )
            with mock.patch.object(
                MODULE.os,
                "lstat",
                side_effect=[original_stat, replacement_stat],
            ):
                with self.assertRaisesRegex(CapacityAssemblyError, "changed while reading"):
                    MODULE.load_trials_snapshot(path)

    def test_canonical_validator_rejects_invalid_boundary(self) -> None:
        with self.assertRaisesRegex(CapacityAssemblyError, "assembled report is invalid"):
            MODULE.assemble_report(
                trials=[trial(1, "pass"), trial(4, "pass")],
                run_id=str(uuid.uuid4()),
                node_profile="linux-x86_64 4 vCPU 8 GiB",
                software_revision="a" * 40,
                scenario="fixture",
                safety_margin_percent=25,
                notes="",
            )

    def test_main_writes_exclusive_validated_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "trials.jsonl"
            output = root / "report.json"
            raw.write_text(
                "\n".join(
                    json.dumps(item, separators=(",", ":"))
                    for item in (
                        trial(1, "pass"),
                        trial(4, "pass"),
                        trial(8, "fail", failed_sessions=1),
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            argv = [
                "--trials-jsonl",
                str(raw),
                "--run-id",
                str(uuid.uuid4()),
                "--node-profile",
                "linux-x86_64 4 vCPU 8 GiB",
                "--software-revision",
                "b" * 40,
                "--scenario",
                "fixture scenario",
                "--safety-margin-percent",
                "25",
                "--output",
                str(output),
            ]
            with mock.patch.object(
                MODULE,
                "_fsync_directory",
                wraps=MODULE._fsync_directory,
            ) as sync_directory:
                self.assertEqual(MODULE.main(argv), 0)
            self.assertEqual(sync_directory.call_count, 1)
            rendered = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(len(rendered["trials"]), 3)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            before = output.read_bytes()
            self.assertEqual(MODULE.main(argv), 2)
            self.assertEqual(output.read_bytes(), before)

    def test_atomic_publish_failure_does_not_leave_partial_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "trials.jsonl"
            output = root / "report.json"
            raw.write_text(
                "\n".join(
                    json.dumps(item, separators=(",", ":"))
                    for item in (
                        trial(1, "pass"),
                        trial(4, "pass"),
                        trial(8, "fail", failed_sessions=1),
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            argv = [
                "--trials-jsonl",
                str(raw),
                "--run-id",
                str(uuid.uuid4()),
                "--node-profile",
                "linux-x86_64 4 vCPU 8 GiB",
                "--software-revision",
                "d" * 40,
                "--scenario",
                "fixture scenario",
                "--safety-margin-percent",
                "25",
                "--output",
                str(output),
            ]
            with mock.patch.object(MODULE.os, "link", side_effect=OSError("injected")):
                self.assertEqual(MODULE.main(argv), 2)
            self.assertFalse(output.exists())
            self.assertEqual(list(root.glob(".report.json.tmp-*")), [])

    def test_directory_sync_failure_rolls_back_published_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "report.json"
            with mock.patch.object(
                MODULE,
                "_fsync_directory",
                side_effect=[CapacityAssemblyError("injected directory sync"), None],
            ) as sync_directory:
                with self.assertRaisesRegex(
                    CapacityAssemblyError,
                    "cannot make output publication durable",
                ):
                    MODULE._write_exclusive_atomic(output, "{}\n")

            self.assertEqual(sync_directory.call_count, 2)
            self.assertFalse(output.exists())
            self.assertEqual(list(root.glob(".report.json.tmp-*")), [])

    def test_main_refuses_to_overwrite_raw_trials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "trials.jsonl"
            raw.write_text("{}\n", encoding="utf-8")
            status = MODULE.main(
                [
                    "--trials-jsonl",
                    str(raw),
                    "--run-id",
                    str(uuid.uuid4()),
                    "--node-profile",
                    "profile",
                    "--software-revision",
                    "c" * 40,
                    "--scenario",
                    "scenario",
                    "--safety-margin-percent",
                    "25",
                    "--output",
                    str(raw),
                ]
            )
            self.assertEqual(status, 2)
            self.assertEqual(raw.read_text(encoding="utf-8"), "{}\n")


if __name__ == "__main__":
    unittest.main()
