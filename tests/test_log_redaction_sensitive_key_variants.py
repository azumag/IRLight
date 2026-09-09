from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "apps" / "control-api" / "log_redaction_inspect_cli.py"
spec = importlib.util.spec_from_file_location("log_redaction_inspect_cli_sensitive_variants", MODULE_PATH)
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


class LogRedactionSensitiveKeyVariantTests(unittest.TestCase):
    def inspect(self, value: object) -> dict[str, object]:
        return module.inspect_lines([json.dumps(value) + "\n"])

    def test_namespaced_sensitive_field_suffixes_require_redaction(self) -> None:
        result = self.inspect(
            record(
                payload={
                    "oauth2AccessToken": "raw",
                    "x-api-key": "raw",
                    "proxy_authorization": "raw",
                    "databasePassword": "raw",
                    "relayStreamKey": "raw",
                    "publisherPassphrase": "raw",
                    "sessionCookie": "raw",
                }
            )
        )
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertEqual(result["violations"], {"SENSITIVE_FIELD_UNREDACTED": 7})

    def test_namespaced_sensitive_url_params_require_redaction(self) -> None:
        result = self.inspect(
            record(
                callback=(
                    "/callback?oauth2AccessToken=raw&x-api-key=raw"
                    "#relayStreamKey=raw&databasePassword=raw"
                )
            )
        )
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertEqual(
            result["violations"],
            {
                "SENSITIVE_URL_FRAGMENT_UNREDACTED": 1,
                "SENSITIVE_URL_QUERY_UNREDACTED": 1,
            },
        )

    def test_redacted_namespaced_sensitive_values_are_allowed(self) -> None:
        result = self.inspect(
            record(
                payload={
                    "oauth2AccessToken": "[REDACTED]",
                    "x-api-key": None,
                    "databasePassword": "***",
                },
                callback="?proxyAuthorization=%3Credacted%3E",
            )
        )
        self.assertEqual(result, {"status": "SAFE", "records": 1, "violations": {}})

    def test_non_sensitive_suffix_lookalikes_remain_allowed(self) -> None:
        result = self.inspect(
            record(
                metrics={
                    "token_count": 12,
                    "password_policy": "strict",
                    "cookie_count": 3,
                    "api_key_rotation_count": 4,
                }
            )
        )
        self.assertEqual(result, {"status": "SAFE", "records": 1, "violations": {}})


if __name__ == "__main__":
    unittest.main()
