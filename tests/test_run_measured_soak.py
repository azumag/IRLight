from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run-measured-soak.py"
SPEC = importlib.util.spec_from_file_location("run_measured_soak", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


SAMPLE_ZERO = {
    "elapsed_seconds": 0,
    "memory_rss_bytes": 100,
    "cpu_percent": 1.0,
    "open_fds": 10,
    "processes": 4,
    "zombies": 0,
    "bitrate_bps": 1_000_000,
    "av_sync_drift_ms": 0.0,
    "timestamp_errors": 0,
    "unexpected_reconnects": 0,
}
SAMPLE_FINAL = {
    **SAMPLE_ZERO,
    "elapsed_seconds": 60,
    "memory_rss_bytes": 110,
}


class MeasuredSoakRunnerTest(unittest.TestCase):
    def _args(
        self,
        *,
        output_dir: Path,
        media_metrics_file: Path | None,
        allow_unmeasured_media: bool = False,
    ) -> argparse.Namespace:
        return argparse.Namespace(
            output_dir=output_dir,
            scenario="unit-test",
            duration_seconds=60,
            interval_seconds=30,
            notes="synthetic runner test",
            media_metrics_file=media_metrics_file,
            allow_unmeasured_media=allow_unmeasured_media,
        )

    @staticmethod
    def _write_samples(path: Path, *samples: dict[str, object]) -> None:
        with path.open("x", encoding="utf-8") as handle:
            for sample in samples:
                handle.write(json.dumps(sample, sort_keys=True) + "\n")

    def test_success_requires_soak_cleanup_and_validated_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "scripts").mkdir()
            metrics = root / "media.json"
            metrics.write_text("{}\n", encoding="utf-8")
            output = root / "evidence"
            calls: list[list[str]] = []

            def fake_run(command, **kwargs):
                calls.append(list(command))
                if command[0] == "bash":
                    env = kwargs["env"]
                    self.assertEqual(env["SOAK_SECONDS"], "60")
                    self.assertEqual(env["SOAK_INTERVAL_SECONDS"], "30")
                    self.assertEqual(env["SOAK_MEDIA_METRICS_FILE"], str(metrics.resolve()))
                    self.assertNotIn("SOAK_ALLOW_UNMEASURED_MEDIA", env)
                    self._write_samples(
                        Path(env["SOAK_SAMPLES_JSONL"]), SAMPLE_ZERO, SAMPLE_FINAL
                    )
                    return subprocess.CompletedProcess(command, 0)
                if command[1].endswith("assemble-soak-report.py"):
                    report = Path(command[command.index("--output") + 1])
                    report.write_text("{}\n", encoding="utf-8")
                    self.assertIn("--cleanup-verified", command)
                    self.assertEqual(command[command.index("--outcome") + 1], "pass")
                    return subprocess.CompletedProcess(command, 0)
                if command[1].endswith("validate-soak-report.py"):
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout=json.dumps({"outcome": "pass", "cleanup_verified": True}),
                        stderr="",
                    )
                self.fail(f"unexpected command: {command}")

            with patch.object(runner, "_repo_root", return_value=root), patch.object(
                runner.subprocess, "run", side_effect=fake_run
            ):
                result = runner.run(
                    self._args(output_dir=output, media_metrics_file=metrics)
                )

            self.assertEqual(result, 0)
            receipt = json.loads((output / "run-result.json").read_text(encoding="utf-8"))
            self.assertTrue(receipt["cleanup_verified"])
            self.assertEqual(receipt["media_mode"], "measured")
            self.assertEqual(receipt["report_file"], "report.json")
            self.assertEqual(receipt["summary_file"], "summary.json")
            self.assertIsNone(receipt["evidence_error"])
            self.assertEqual(len(calls), 3)

    def test_failed_soak_never_claims_cleanup_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "scripts").mkdir()
            output = root / "evidence"

            def fake_run(command, **kwargs):
                if command[0] == "bash":
                    env = kwargs["env"]
                    self.assertEqual(env["SOAK_ALLOW_UNMEASURED_MEDIA"], "1")
                    self.assertNotIn("SOAK_MEDIA_METRICS_FILE", env)
                    self._write_samples(Path(env["SOAK_SAMPLES_JSONL"]), SAMPLE_ZERO)
                    return subprocess.CompletedProcess(command, 7)
                if command[1].endswith("assemble-soak-report.py"):
                    report = Path(command[command.index("--output") + 1])
                    report.write_text("{}\n", encoding="utf-8")
                    self.assertNotIn("--cleanup-verified", command)
                    self.assertEqual(command[command.index("--outcome") + 1], "fail")
                    return subprocess.CompletedProcess(command, 0)
                if command[1].endswith("validate-soak-report.py"):
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout=json.dumps({"outcome": "fail", "cleanup_verified": False}),
                        stderr="",
                    )
                self.fail(f"unexpected command: {command}")

            with patch.object(runner, "_repo_root", return_value=root), patch.object(
                runner.subprocess, "run", side_effect=fake_run
            ):
                result = runner.run(
                    self._args(
                        output_dir=output,
                        media_metrics_file=None,
                        allow_unmeasured_media=True,
                    )
                )

            self.assertEqual(result, 7)
            receipt = json.loads((output / "run-result.json").read_text(encoding="utf-8"))
            self.assertFalse(receipt["cleanup_verified"])
            self.assertEqual(receipt["media_mode"], "diagnostic-unmeasured")
            self.assertEqual(receipt["report_file"], "report.json")

    def test_success_without_samples_is_an_evidence_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "scripts").mkdir()
            metrics = root / "media.json"
            metrics.write_text("{}\n", encoding="utf-8")
            output = root / "evidence"

            with patch.object(runner, "_repo_root", return_value=root), patch.object(
                runner.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(["bash"], 0),
            ):
                with self.assertRaisesRegex(
                    runner.MeasuredSoakError, "no sample evidence was produced"
                ):
                    runner.run(self._args(output_dir=output, media_metrics_file=metrics))

            receipt = json.loads((output / "run-result.json").read_text(encoding="utf-8"))
            self.assertEqual(receipt["soak_exit_code"], 0)
            self.assertFalse(receipt["report_file"])
            self.assertEqual(receipt["evidence_error"], "no sample evidence was produced")

    def test_existing_output_directory_is_never_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "evidence"
            output.mkdir()
            with self.assertRaisesRegex(runner.MeasuredSoakError, "already exists"):
                runner._prepare_paths(output)

    def test_interval_cannot_exceed_duration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            metrics = root / "media.json"
            metrics.write_text("{}\n", encoding="utf-8")
            args = self._args(output_dir=root / "evidence", media_metrics_file=metrics)
            args.interval_seconds = 61
            with self.assertRaisesRegex(runner.MeasuredSoakError, "must not exceed"):
                runner.run(args)


if __name__ == "__main__":
    unittest.main()
