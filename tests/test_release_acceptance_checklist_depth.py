from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validate-release-acceptance-checklist.py"

_spec = importlib.util.spec_from_file_location(
    "release_acceptance_checklist_depth", SCRIPT
)
assert _spec is not None and _spec.loader is not None
module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(module)


class ReleaseAcceptanceChecklistDepthTests(unittest.TestCase):
    def test_deeply_nested_json_is_reported_as_validation_error(self) -> None:
        fd, name = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        path = Path(name)
        self.addCleanup(path.unlink, missing_ok=True)

        depth = 10_000
        raw = ("[" * depth + "0" + "]" * depth).encode("utf-8")
        self.assertLess(len(raw), module.MAX_CHECKLIST_BYTES)
        path.write_bytes(raw)

        with self.assertRaisesRegex(
            module.ChecklistValidationError, "checklist is not valid JSON"
        ):
            module.load_checklist(path)


if __name__ == "__main__":
    unittest.main()
