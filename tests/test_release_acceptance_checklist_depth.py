from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validate-release-acceptance-checklist.py"

_spec = importlib.util.spec_from_file_location(
    "release_acceptance_checklist_depth", SCRIPT
)
assert _spec is not None and _spec.loader is not None
module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(module)


class ReleaseAcceptanceChecklistDepthTests(unittest.TestCase):
    def _write_temp(self, raw: bytes) -> Path:
        fd, name = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        path = Path(name)
        self.addCleanup(path.unlink, missing_ok=True)
        path.write_bytes(raw)
        return path

    def test_parser_recursion_error_is_normalized(self) -> None:
        path = self._write_temp(b"{}")

        with mock.patch.object(
            module.json, "loads", side_effect=RecursionError("too deep")
        ):
            with self.assertRaisesRegex(
                module.ChecklistValidationError, "checklist is not valid JSON"
            ):
                module.load_checklist(path)

    def test_parser_value_error_is_normalized(self) -> None:
        path = self._write_temp(b"{}")

        with mock.patch.object(
            module.json, "loads", side_effect=ValueError("integer string too long")
        ):
            with self.assertRaisesRegex(
                module.ChecklistValidationError, "checklist is not valid JSON"
            ):
                module.load_checklist(path)

    def test_deeply_nested_json_fails_closed(self) -> None:
        depth = 10_000
        raw = ("[" * depth + "0" + "]" * depth).encode("utf-8")
        self.assertLess(len(raw), module.MAX_CHECKLIST_BYTES)
        path = self._write_temp(raw)

        with self.assertRaises(module.ChecklistValidationError):
            module.load_checklist(path)


if __name__ == "__main__":
    unittest.main()
