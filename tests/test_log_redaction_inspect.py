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

    def test_camel_case_sensitive_keys_are_normalized(self) -> None:
        result = self.inspect(
            record(payload={"accessToken": "raw", "streamKey": "raw", "clientSecret": "raw"})
        )
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertEqual(result["violations"], {"SENSITIVE_FIELD_UNREDACTED": 3})

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

    def test_relative_url_sensitive_query_is_reported(self) -> None:
        result = self.inspect(
            record(
                callback="/callback?apiKey=raw",
                next_url="?refreshToken=raw",
            )
        )
        self.assertEqual(
            result["violations"], {"SENSITIVE_URL_QUERY_UNREDACTED": 2}
        )

    def test_sensitive_url_fragment_is_reported_without_url(self) -> None:
        url = "https://example.invalid/callback#access_token=super-secret&token_type=bearer"
        result = self.inspect(record(callback=url))
        encoded = json.dumps(result)
        self.assertEqual(
            result["violations"], {"SENSITIVE_URL_FRAGMENT_UNREDACTED": 1}
        )
        self.assertNotIn("super-secret", encoded)
        self.assertNotIn("example.invalid", encoded)

    def test_relative_sensitive_url_fragment_is_reported(self) -> None:
        result = self.inspect(record(callback="#refreshToken=raw"))
        self.assertEqual(
            result["violations"], {"SENSITIVE_URL_FRAGMENT_UNREDACTED": 1}
        )

    def test_redacted_sensitive_url_fragment_is_allowed(self) -> None:
        result = self.inspect(
            record(callback="https://example.invalid/callback#accessToken=%5BREDACTED%5D")
        )
        self.assertEqual(result["status"], "SAFE")

    def test_url_userinfo_is_reported_without_url(self) -> None:
        url = "https://relay-user:relay-password@example.invalid/live"
        result = self.inspect(record(destination=url))
        encoded = json.dumps(result)
        self.assertEqual(result["violations"], {"SENSITIVE_URL_USERINFO": 1})
        self.assertNotIn("relay-user", encoded)
        self.assertNotIn("relay-password", encoded)
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

    def test_oversized_record_is_invalid_without_parsing_content(self) -> None:
        secret = "AUDIT_DUMMY_SECRET"
        line = json.dumps(record(message=secret + ("x" * module._MAX_RECORD_BYTES))) + "\n"
        result = module.inspect_lines([line])
        encoded = json.dumps(result)
        self.assertEqual(result["status"], "INVALID")
        self.assertEqual(result["violations"], {"RECORD_TOO_LARGE": 1})
        self.assertNotIn(secret, encoded)

    def test_oversized_blank_record_is_invalid_before_strip(self) -> None:
        line = (" " * (module._MAX_RECORD_BYTES + 1)) + "\n"
        result = module.inspect_lines([line])
        self.assertEqual(result["status"], "INVALID")
        self.assertEqual(result["records"], 1)
        self.assertEqual(result["violations"], {"RECORD_TOO_LARGE": 1})

    def test_main_reads_oversized_line_in_bounded_chunks(self) -> None:
        class GuardedStream(io.StringIO):
            def __init__(self, value: str) -> None:
                super().__init__(value)
                self.max_requested = 0

            def __iter__(self):
                raise AssertionError("main must not iterate stdin with unbounded line reads")

            def readline(self, size: int = -1) -> str:
                if size < 0:
                    raise AssertionError("readline must be bounded")
                self.max_requested = max(self.max_requested, size)
                return super().readline(size)

        secret = "AUDIT_DUMMY_SECRET"
        stream = GuardedStream(secret + ("x" * (module._MAX_RECORD_BYTES * 2)) + "\n")
        original_stdout = sys.stdout
        stdout = io.StringIO()
        try:
            sys.stdout = stdout
            code = module.main([], stdin=stream)
        finally:
            sys.stdout = original_stdout
        result = json.loads(stdout.getvalue())
        self.assertEqual(code, 3)
        self.assertEqual(result["violations"], {"RECORD_TOO_LARGE": 1})
        self.assertLessEqual(stream.max_requested, module._MAX_RECORD_BYTES + 1)
        self.assertNotIn(secret, stdout.getvalue())

    def test_binary_reader_applies_limit_to_multibyte_utf8_bytes(self) -> None:
        class GuardedBytesStream(io.BytesIO):
            def __init__(self, value: bytes) -> None:
                super().__init__(value)
                self.max_requested = 0

            def readline(self, size: int = -1) -> bytes:
                if size < 0:
                    raise AssertionError("binary readline must be bounded")
                self.max_requested = max(self.max_requested, size)
                return super().readline(size)

        raw = ("界" * (module._MAX_RECORD_BYTES // 3 + 100)).encode("utf-8") + b"\n"
        stream = GuardedBytesStream(raw)
        result = module.inspect_lines(module._iter_bounded_byte_lines(stream))
        self.assertEqual(result["status"], "INVALID")
        self.assertEqual(result["records"], 1)
        self.assertEqual(result["violations"], {"RECORD_TOO_LARGE": 1})
        self.assertLessEqual(stream.max_requested, module._MAX_RECORD_BYTES + 1)

    def test_main_uses_raw_buffer_and_normalizes_invalid_utf8(self) -> None:
        stream = io.TextIOWrapper(io.BytesIO(b"\xff\n"), encoding="utf-8")
        original_stdout = sys.stdout
        stdout = io.StringIO()
        try:
            sys.stdout = stdout
            code = module.main([], stdin=stream)
        finally:
            sys.stdout = original_stdout
        result = json.loads(stdout.getvalue())
        self.assertEqual(code, 3)
        self.assertEqual(result["records"], 1)
        self.assertEqual(result["violations"], {"INVALID_JSON": 1})

    def test_excessive_nesting_is_invalid_without_recursion(self) -> None:
        nested: object = "leaf"
        for _ in range(module._MAX_NESTING_DEPTH + 2):
            nested = {"child": nested}
        result = self.inspect(record(payload=nested))
        self.assertEqual(result["status"], "INVALID")
        self.assertEqual(result["violations"], {"NESTING_TOO_DEEP": 1})

    def test_parser_recursion_error_is_normalized(self) -> None:
        depth = 2000
        line = (
            '{"timestamp":"x","level":"INFO","service":"cp","event_type":"x","payload":'
            + ("[" * depth)
            + "0"
            + ("]" * depth)
            + "}\n"
        )
        result = module.inspect_lines([line])
        self.assertEqual(result["status"], "INVALID")
        self.assertEqual(result["violations"], {"NESTING_TOO_DEEP": 1})

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
