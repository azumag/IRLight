from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "apps" / "control-api" / "log_redaction_inspect_cli.py"
spec = importlib.util.spec_from_file_location("log_redaction_inspect_cli_cloud_signed_urls", MODULE_PATH)
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


class LogRedactionCloudSignedUrlParameterTests(unittest.TestCase):
    def inspect(self, value: object) -> dict[str, object]:
        return module.inspect_lines([json.dumps(value) + "\n"])

    def test_gcs_v4_signed_query_requires_redaction(self) -> None:
        result = self.inspect(
            record(
                url=(
                    "https://storage.googleapis.com/example/object"
                    "?X-Goog-Algorithm=GOOG4-RSA-SHA256"
                    "&X-Goog-Credential=service%40example.iam.gserviceaccount.com%2Fscope"
                    "&X-Goog-Signature=deadbeef"
                )
            )
        )
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertEqual(result["violations"], {"SENSITIVE_URL_QUERY_UNREDACTED": 1})

    def test_gcs_v4_fragment_variants_require_redaction(self) -> None:
        result = self.inspect(
            record(url="#xGoogCredential=credential&x_goog_signature=signature")
        )
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertEqual(result["violations"], {"SENSITIVE_URL_FRAGMENT_UNREDACTED": 1})

    def test_gcs_v4_redacted_parameters_are_allowed(self) -> None:
        result = self.inspect(
            record(
                url=(
                    "?X-Goog-Credential=%5BREDACTED%5D"
                    "&X-Goog-Signature=***"
                )
            )
        )
        self.assertEqual(result, {"status": "SAFE", "records": 1, "violations": {}})

    def test_gcs_v2_signature_requires_redaction_only_in_signed_url_context(self) -> None:
        result = self.inspect(
            record(
                url=(
                    "https://storage.googleapis.com/example/object"
                    "?GoogleAccessId=service%40example.iam.gserviceaccount.com"
                    "&Expires=1788973200&Signature=deadbeef"
                )
            )
        )
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertEqual(result["violations"], {"SENSITIVE_URL_QUERY_UNREDACTED": 1})

    def test_generic_signature_query_is_not_treated_as_gcs_v2(self) -> None:
        result = self.inspect(
            record(url="/render?Expires=1788973200&Signature=layout-v2")
        )
        self.assertEqual(result, {"status": "SAFE", "records": 1, "violations": {}})

    def test_azure_sas_signature_requires_redaction(self) -> None:
        result = self.inspect(
            record(
                url=(
                    "https://example.blob.core.windows.net/container/blob"
                    "?sv=2025-11-05&se=2026-09-09T23%3A00%3A00Z"
                    "&sp=r&sr=b&sig=deadbeef"
                )
            )
        )
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertEqual(result["violations"], {"SENSITIVE_URL_QUERY_UNREDACTED": 1})

    def test_azure_sas_ad_hoc_access_context_requires_redaction(self) -> None:
        result = self.inspect(
            record(url="?sv=2025-11-05&sp=r&se=2026-09-09T23%3A00%3A00Z&sig=deadbeef")
        )
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertEqual(result["violations"], {"SENSITIVE_URL_QUERY_UNREDACTED": 1})

    def test_azure_sas_fragment_signature_requires_redaction(self) -> None:
        result = self.inspect(
            record(url="#sv=2025-11-05&si=read-policy&sr=b&sig=deadbeef")
        )
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertEqual(result["violations"], {"SENSITIVE_URL_FRAGMENT_UNREDACTED": 1})

    def test_redacted_azure_sas_signature_is_allowed(self) -> None:
        result = self.inspect(
            record(url="?sv=2025-11-05&se=2026-09-09T23%3A00%3A00Z&sp=r&sr=b&sig=%3Credacted%3E")
        )
        self.assertEqual(result, {"status": "SAFE", "records": 1, "violations": {}})

    def test_generic_sig_query_is_not_treated_as_azure_sas(self) -> None:
        for url in (
            "/callback?sv=2&sig=checksum",
            "/callback?sv=2&se=tomorrow&sig=checksum",
            "/callback?sp=r&se=tomorrow&sig=checksum",
        ):
            with self.subTest(url=url):
                result = self.inspect(record(url=url))
                self.assertEqual(result, {"status": "SAFE", "records": 1, "violations": {}})

    def test_provider_specific_names_do_not_expand_field_secret_policy(self) -> None:
        result = self.inspect(
            record(
                metadata={
                    "x_goog_signature": "diagnostic-name-only",
                    "signature": "layout-v2",
                    "sig": "checksum",
                }
            )
        )
        self.assertEqual(result, {"status": "SAFE", "records": 1, "violations": {}})


if __name__ == "__main__":
    unittest.main()
