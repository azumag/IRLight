from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validate-manual-compatibility-reports.py"

_spec = importlib.util.spec_from_file_location("manual_compatibility_reports_matrix", SCRIPT)
assert _spec is not None and _spec.loader is not None
validator = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(validator)


class ManualCompatibilityMatrixInputTest(unittest.TestCase):
    def _load_raw(self, raw: str):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "matrix.json"
            path.write_text(raw, encoding="utf-8")
            return validator.load_compatibility_matrix(path)

    def test_rejects_duplicate_matrix_object_keys(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate object key is not allowed"):
            self._load_raw('{"entries":[],"entries":[]}')

    def test_rejects_nonstandard_matrix_json_constants(self) -> None:
        with self.assertRaisesRegex(
            ValueError, "non-standard JSON constant is not allowed: NaN"
        ):
            self._load_raw('{"entries":[],"padding":NaN}')

    def test_rejects_oversized_matrix_before_parsing(self) -> None:
        raw = '{"padding":"' + ("x" * validator.MAX_COMPATIBILITY_MATRIX_BYTES) + '"}'
        with self.assertRaisesRegex(
            ValueError,
            f"compatibility matrix exceeds {validator.MAX_COMPATIBILITY_MATRIX_BYTES}-byte limit",
        ):
            self._load_raw(raw)


if __name__ == "__main__":
    unittest.main()
