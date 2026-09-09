from __future__ import annotations

import importlib.util
import io
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CONTROL_API_ROOT = REPO_ROOT / "apps" / "control-api"
MODULE_PATH = CONTROL_API_ROOT / "operations_jsonl_safety.py"

if str(CONTROL_API_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROL_API_ROOT))

spec = importlib.util.spec_from_file_location("operations_jsonl_safety", MODULE_PATH)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class OperationsJsonlSafetyTests(unittest.TestCase):
    def test_raw_multibyte_limit_is_applied_before_decode(self) -> None:
        payload = ("あ" * 5).encode("utf-8") + b"\n"
        lines = list(
            module.iter_bounded_byte_lines(
                io.BytesIO(payload), max_record_bytes=10
            )
        )
        self.assertEqual(lines, [module.OVERSIZED_RECORD])

    def test_oversized_line_is_drained_as_one_record(self) -> None:
        payload = b"x" * 40 + b"\n{}\n"
        lines = list(
            module.iter_bounded_byte_lines(
                io.BytesIO(payload), max_record_bytes=8
            )
        )
        self.assertIs(lines[0], module.OVERSIZED_RECORD)
        self.assertEqual(lines[1], "{}\n")
        self.assertEqual(len(lines), 2)

    def test_invalid_utf8_uses_stable_sentinel(self) -> None:
        lines = list(
            module.iter_bounded_byte_lines(
                io.BytesIO(b"\xff\xfe\n"), max_record_bytes=8
            )
        )
        self.assertEqual(lines, [module.INVALID_UTF8_RECORD])

    def test_strict_json_rejects_duplicate_keys_and_nonfinite_values(self) -> None:
        with self.assertRaises(module.DuplicateKeyError):
            module.strict_json_loads('{"a":1,"a":2}')
        with self.assertRaises(module.NonFiniteNumberError):
            module.strict_json_loads('{"a":NaN}')

    def test_text_stream_multibyte_size_is_checked_after_read(self) -> None:
        line = "あ" * 4
        self.assertEqual(
            module.utf8_size_violation(line, max_record_bytes=10),
            "RECORD_TOO_LARGE",
        )
        self.assertIsNone(module.utf8_size_violation("abc", max_record_bytes=10))

    def test_nonpositive_record_limit_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            list(module.iter_bounded_byte_lines(io.BytesIO(b"{}\n"), max_record_bytes=0))
        with self.assertRaises(ValueError):
            list(module.iter_bounded_lines(io.StringIO("{}\n"), max_record_bytes=-1))


if __name__ == "__main__":
    unittest.main()
