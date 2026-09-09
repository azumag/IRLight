from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "apps" / "control-api" / "log_redaction_inspect_cli.py"
spec = importlib.util.spec_from_file_location("log_redaction_inspect_cli_presigned_urls", MODULE_PATH)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def record(**extra: object) -> dict[str, object]:
    value: dict[str, object] = {
        "timestamp": "2026-09-09T00:00:00Z",
        "level": "INFO",
        "service": "control-api",
        "event_type": "asset.fetch",
    }
    value.update(extra)
    return value


class LogRedactionPresignedUrlParameterTests(unittest.TestCase):
    def inspect(self, value: object) -> dict[str, object]:
        return module.inspect_lines([json.dumps(value) + "\n"])

    def test_aws_sigv4_presigned_query_requires_redaction(self) -> None:
        result = self.inspect(
            record(
                url=(
                    "https://example.invalid/object?X-Amz-Algorithm=AWS4-HMAC-SHA256"
                    "&X-Amz-Credential=AKIAEXAMPLE%2F20260909%2Fap-northeast-1%2Fs3%2Faws4_request"
                    "&X-Amz-Signature=deadbeef&X-Amz-Security-Token=session-token"
                )
            )
        )
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertEqual(result["violations"], {"SENSITIVE_URL_QUERY_UNREDACTED": 1})

    def test_presigned_fragment_variants_require_redaction(self) -> None:
        result = self.inspect(
            record(url="#xAmzCredential=credential&x_amz_signature=signature")
        )
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertEqual(result["violations"], {"SENSITIVE_URL_FRAGMENT_UNREDACTED": 1})

    def test_redacted_presigned_parameters_are_allowed(self) -> None:
        result = self.inspect(
            record(
                url=(
                    "?X-Amz-Credential=%5BREDACTED%5D"
                    "&X-Amz-Signature=***"
                    "&X-Amz-Security-Token=%3Credacted%3E"
                )
            )
        )
        self.assertEqual(result, {"status": "SAFE", "records": 1, "violations": {}})

    def test_signature_and_credential_metadata_are_not_globally_sensitive(self) -> None:
        result = self.inspect(
            record(
                metadata={
                    "signature_algorithm": "ed25519",
                    "credential_format": "scoped",
                    "x_amz_signature": "diagnostic-name-only",
                },
                url=(
                    "/object?signature_algorithm=AWS4-HMAC-SHA256"
                    "&credential_format=scoped&X-Amz-Date=20260909T000000Z"
                    "&X-Amz-Expires=300&X-Amz-SignedHeaders=host"
                ),
            )
        )
        self.assertEqual(result, {"status": "SAFE", "records": 1, "violations": {}})


if __name__ == "__main__":
    unittest.main()
