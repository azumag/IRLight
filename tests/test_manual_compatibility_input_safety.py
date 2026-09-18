from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validate-manual-compatibility-reports.py"
_spec = importlib.util.spec_from_file_location(
    "manual_compatibility_reports_safety", SCRIPT
)
assert _spec is not None and _spec.loader is not None
validator = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(validator)


def report() -> dict[str, object]:
    return {
        "schema_version": 1,
        "entry_id": "obs-real-device",
        "coverage": "pc.obs",
        "tested_at": "2026-09-16T05:30:00+09:00",
        "subject": "OBS Studio",
        "version": "32.2.1",
        "environment": "sanitized host",
        "transport": "RTMPS",
        "profile": "1080p30 H.264/AAC",
        "network": "wired LAN",
        "result": "PASS",
        "checks": [{"name": "publish accepted", "result": "PASS"}],
        "notes": "sanitized",
    }


def matrix(evidence: str) -> dict[str, object]:
    return {
        "entries": [
            {
                "id": "obs-real-device",
                "coverage": "pc.obs",
                "status": "manual_verified",
                "evidence": [evidence],
            }
        ]
    }


class ManualInputSafetyTest(unittest.TestCase):
    def test_matrix_final_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.json"
            target.write_text('{"entries": []}', encoding="utf-8")
            link = root / "matrix.json"
            link.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "regular file"):
                validator.load_compatibility_matrix(link)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO support unavailable")
    def test_matrix_fifo_is_rejected_without_opening(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fifo = Path(directory) / "matrix.fifo"
            os.mkfifo(fifo)
            with self.assertRaisesRegex(ValueError, "regular file"):
                validator.load_compatibility_matrix(fifo)

    def test_matrix_invalid_utf8_and_recursive_json_are_controlled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "matrix.json"
            path.write_bytes(b"\xff")
            with self.assertRaisesRegex(ValueError, "invalid UTF-8"):
                validator.load_compatibility_matrix(path)
            path.write_text("{}", encoding="utf-8")
            with mock.patch.object(
                validator.json, "loads", side_effect=RecursionError("synthetic")
            ):
                with self.assertRaisesRegex(ValueError, "cannot read compatibility matrix"):
                    validator.load_compatibility_matrix(path)

    def test_matrix_path_replacement_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "matrix.json"
            replacement = root / "replacement.json"
            path.write_text('{"entries": []}', encoding="utf-8")
            replacement.write_text('{"entries": []}', encoding="utf-8")
            original_stat = os.lstat(path)
            replacement_stat = os.lstat(replacement)
            self.assertNotEqual(
                (original_stat.st_dev, original_stat.st_ino),
                (replacement_stat.st_dev, replacement_stat.st_ino),
            )
            with mock.patch.object(
                validator.os, "lstat", side_effect=[original_stat, replacement_stat]
            ):
                with self.assertRaisesRegex(ValueError, "changed while reading"):
                    validator.load_compatibility_matrix(path)

    def test_matrix_in_place_mutation_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "matrix.json"
            path.write_text('{"entries": []}', encoding="utf-8")
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
                validator.os, "fstat", side_effect=[stable, stable, mutated]
            ):
                with self.assertRaisesRegex(ValueError, "changed while reading"):
                    validator.load_compatibility_matrix(path)

    def test_manual_report_final_symlink_inside_report_dir_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report_dir = root / "docs" / "compatibility-reports"
            report_dir.mkdir(parents=True)
            target = report_dir / "target.json"
            target.write_text(json.dumps(report()), encoding="utf-8")
            link = report_dir / "obs.json"
            link.symlink_to(target.name)
            errors = validator.validate_matrix_manual_reports(
                matrix("docs/compatibility-reports/obs.json"), root=root
            )
            self.assertTrue(
                any("must be a regular file" in error for error in errors), errors
            )

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO support unavailable")
    def test_manual_report_fifo_is_rejected_without_opening(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report_dir = root / "docs" / "compatibility-reports"
            report_dir.mkdir(parents=True)
            fifo = report_dir / "obs.json"
            os.mkfifo(fifo)
            errors = validator.validate_matrix_manual_reports(
                matrix("docs/compatibility-reports/obs.json"), root=root
            )
            self.assertTrue(
                any("must be a regular file" in error for error in errors), errors
            )

    def test_manual_report_path_replacement_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report_dir = root / "docs" / "compatibility-reports"
            report_dir.mkdir(parents=True)
            path = report_dir / "obs.json"
            replacement = report_dir / "replacement.json"
            payload = json.dumps(report())
            path.write_text(payload, encoding="utf-8")
            replacement.write_text(payload, encoding="utf-8")
            original_stat = os.lstat(path)
            replacement_stat = os.lstat(replacement)
            real_lstat = validator.os.lstat
            target_calls = 0

            def fake_lstat(candidate):
                nonlocal target_calls
                if Path(candidate) == path:
                    target_calls += 1
                    return original_stat if target_calls == 1 else replacement_stat
                return real_lstat(candidate)

            with mock.patch.object(validator.os, "lstat", side_effect=fake_lstat):
                errors = validator.validate_matrix_manual_reports(
                    matrix("docs/compatibility-reports/obs.json"), root=root
                )
            self.assertTrue(
                any("changed while reading" in error for error in errors), errors
            )

    def test_manual_report_in_place_mutation_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report_dir = root / "docs" / "compatibility-reports"
            report_dir.mkdir(parents=True)
            path = report_dir / "obs.json"
            path.write_text(json.dumps(report()), encoding="utf-8")
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
                validator.os, "fstat", side_effect=[stable, stable, mutated]
            ):
                errors = validator.validate_matrix_manual_reports(
                    matrix("docs/compatibility-reports/obs.json"), root=root
                )
            self.assertTrue(
                any("changed while reading" in error for error in errors), errors
            )

    def test_manual_report_invalid_utf8_and_recursive_json_are_controlled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report_dir = root / "docs" / "compatibility-reports"
            report_dir.mkdir(parents=True)
            path = report_dir / "obs.json"
            path.write_bytes(b"\xff")
            errors = validator.validate_matrix_manual_reports(
                matrix("docs/compatibility-reports/obs.json"), root=root
            )
            self.assertTrue(any("invalid UTF-8" in error for error in errors), errors)
            path.write_text(json.dumps(report()), encoding="utf-8")
            with mock.patch.object(
                validator.json, "loads", side_effect=RecursionError("synthetic")
            ):
                errors = validator.validate_matrix_manual_reports(
                    matrix("docs/compatibility-reports/obs.json"), root=root
                )
            self.assertTrue(
                any("cannot read manual compatibility report" in error for error in errors),
                errors,
            )

    def test_report_recursive_secret_scan_is_controlled(self) -> None:
        value = report()
        with mock.patch.object(
            validator,
            "_sensitive_field_paths",
            side_effect=RecursionError("synthetic"),
        ):
            errors = validator.validate_report(value)
        self.assertIn("report structure is too deeply nested", errors)


if __name__ == "__main__":
    unittest.main()
