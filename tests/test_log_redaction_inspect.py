from __future__ import annotations

import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "apps" / "control-api" / "log_redaction_inspect_cli.py"
spec = importlib.util.spec_from_file_location("log_redaction_inspect_cli", MODULE_PATH)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def record(**extra: object) -> dict[str, object]:
    value: dict[str, object] = {
        "timestamp": "2026-09-09T00:00:00Z",
        "level": "INFO",
        "service": "control-api",
        "event_type": "session.prepare",
    }
    value.update(extra)
    return value


class LogRedactionInspectTests(unittest.TestCase):
    def inspect(self, *values: object) -> dict[str, object]:
        lines = [json.dumps(value) + "\n" for value in values]
        return module.inspect_lines(lines)

    def test_safe_baseline_record_is_accepted(self) -> None:
        result = self.inspect(record(session_id="session-1", reason_code="OK"))
        self.assertEqual(result, {"status": "SAFE", "records": 1, "violations": {}})

    def test_nested_unredacted_secret_is_reported_without_value(self) -> None:
        secret = "do-not-echo-this-token"
        result = self.inspect(record(headers={"Authorization": secret}))
        encoded = json.dumps(result)
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertEqual(result["violations"], {"SENSITIVE_FIELD_UNREDACTED": 1})
        self.assertNotIn(secret, encoded)
        self.assertNotIn("Authorization", encoded)

    def test_redacted_sensitive_fields_are_allowed(self) -> None:
        result = self.inspect(
            record(
                stream_key="[REDACTED]",
                nested={"srt-passphrase": "<redacted>", "token": None},
            )
        )
        self.assertEqual(result["status"], "SAFE")

    def test_sensitive_url_query_is_reported_without_url(self) -> None:
        url = "https://example.invalid/callback?token=super-secret&ok=1"
        result = self.inspect(record(destination=url))
        encoded = json.dumps(result)
        self.assertEqual(
            result["violations"], {"SENSITIVE_URL_QUERY_UNREDACTED": 1}
        )
        self.assertNotIn("super-secret", encoded)
        self.assertNotIn("example.invalid", encoded)

    def test_duplicate_key_and_nonfinite_number_are_invalid(self) -> None:
        prefix = (
            '{"timestamp":"x","level":"INFO","service":"cp",'
            '"event_type":"x",'
        )
        result = module.inspect_lines(
            [
                prefix + '"level":"DEBUG"}\n',
                prefix + '"metric":NaN}\n',
            ]
        )
        self.assertEqual(result["status"], "INVALID")
        self.assertEqual(
            result["violations"],
            {"DUPLICATE_JSON_KEY": 1, "NONFINITE_NUMBER": 1},
        )

    def test_missing_baseline_schema_requires_review(self) -> None:
        result = self.inspect({"message": "hello"})
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertEqual(result["violations"], {"MISSING_REQUIRED_FIELD": 1})

    def test_main_emits_only_summary_and_uses_nonzero_review_exit(self) -> None:
        original_stdout = sys.stdout
        stdout = io.StringIO()
        try:
            sys.stdout = stdout
            code = module.main([], stdin=io.StringIO(json.dumps(record(token="raw"))))
        finally:
            sys.stdout = original_stdout
        self.assertEqual(code, 2)
        self.assertNotIn("raw", stdout.getvalue())
        self.assertEqual(json.loads(stdout.getvalue())["status"], "REVIEW_REQUIRED")


if __name__ == "__main__":
    unittest.main()
