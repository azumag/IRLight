from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "apps" / "control-api" / "log_redaction_inspect_cli.py"
spec = importlib.util.spec_from_file_location("log_redaction_schema_module", MODULE_PATH)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class LogRedactionSchemaTests(unittest.TestCase):
    def test_required_fields_must_be_nonempty_strings(self) -> None:
        result = module.inspect_lines([
            '{"timestamp":null,"level":"","service":42,"event_type":"event"}\n'
        ])
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertEqual(result["violations"], {"INVALID_REQUIRED_FIELD": 1})


if __name__ == "__main__":
    unittest.main()
