from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "validate_soak_report_stable_read",
    ROOT / "scripts" / "validate-soak-report.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

SoakReportError = MODULE.SoakReportError
load_report = MODULE.load_report


class _ReadHookHandle:
    def __init__(self, handle: object, hook: object) -> None:
        self._handle = handle
        self._hook = hook

    def __enter__(self) -> "_ReadHookHandle":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        self._handle.close()
        return False

    def fileno(self) -> int:
        return self._handle.fileno()

    def read(self, size: int = -1) -> bytes:
        value = self._handle.read(size)
        self._hook()
        return value


class ValidateSoakReportStableReadTest(unittest.TestCase):
    def test_loader_rejects_same_inode_mutation_during_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.json"
            path.write_text('{"schema_version":1}', encoding="utf-8")
            original_open = MODULE._open_report_readonly

            def open_with_mutation(target: Path) -> _ReadHookHandle:
                handle = original_open(target)

                def mutate() -> None:
                    with path.open("ab") as writer:
                        writer.write(b" ")
                        writer.flush()
                        os.fsync(writer.fileno())

                return _ReadHookHandle(handle, mutate)

            with mock.patch.object(
                MODULE, "_open_report_readonly", side_effect=open_with_mutation
            ):
                with self.assertRaisesRegex(SoakReportError, "changed while reading"):
                    load_report(path)

    def test_loader_rejects_path_replacement_during_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "report.json"
            replacement = root / "replacement.json"
            path.write_text('{"schema_version":1}', encoding="utf-8")
            replacement.write_text('{"schema_version":1}', encoding="utf-8")
            original_open = MODULE._open_report_readonly

            def open_with_replacement(target: Path) -> _ReadHookHandle:
                handle = original_open(target)
                return _ReadHookHandle(handle, lambda: os.replace(replacement, path))

            with mock.patch.object(
                MODULE, "_open_report_readonly", side_effect=open_with_replacement
            ):
                with self.assertRaisesRegex(SoakReportError, "changed while reading"):
                    load_report(path)

    def test_loader_normalizes_recursive_json_parser_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.json"
            path.write_text("{}", encoding="utf-8")
            with mock.patch.object(
                MODULE.json, "loads", side_effect=RecursionError("too deeply nested")
            ):
                with self.assertRaisesRegex(SoakReportError, "invalid JSON"):
                    load_report(path)


if __name__ == "__main__":
    unittest.main()
