from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validate-manual-compatibility-reports.py"
MATRIX = ROOT / "docs" / "compatibility-matrix.json"

_spec = importlib.util.spec_from_file_location("manual_compatibility_reports", SCRIPT)
assert _spec is not None and _spec.loader is not None
validator = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(validator)


class ManualCompatibilityReportValidationTest(unittest.TestCase):
    def _report(self, **overrides):
        report = {
            "schema_version": 1,
            "entry_id": "obs-real-device",
            "coverage": "pc.obs",
            "tested_at": "2026-09-16T05:30:00+09:00",
            "subject": "OBS Studio",
            "version": "32.2.1",
            "environment": "macOS 15, Apple Silicon test host",
            "transport": "RTMPS",
            "profile": "1080p30 H.264/AAC",
            "network": "wired LAN to local IRLight node",
            "result": "PASS",
            "checks": [
                {"name": "publish accepted", "result": "PASS"},
                {"name": "disconnect and recover", "result": "PASS"},
            ],
            "notes": "Sanitized compatibility evidence; no credentials recorded.",
        }
        report.update(overrides)
        return report

    def _matrix(self, evidence: list[str]) -> dict[str, object]:
        return {
            "entries": [
                {
                    "id": "obs-real-device",
                    "coverage": "pc.obs",
                    "status": "manual_verified",
                    "evidence": evidence,
                }
            ]
        }

    def test_current_matrix_manual_evidence_is_valid(self) -> None:
        matrix = json.loads(MATRIX.read_text(encoding="utf-8"))
        self.assertEqual(validator.validate_matrix_manual_reports(matrix), [])

    def test_valid_report_is_bound_to_matrix_entry_and_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report_path = root / "docs" / "compatibility-reports" / "obs.json"
            report_path.parent.mkdir(parents=True)
            report_path.write_text(json.dumps(self._report()), encoding="utf-8")

            errors = validator.validate_matrix_manual_reports(
                self._matrix(["docs/compatibility-reports/obs.json"]), root=root
            )

        self.assertEqual(errors, [])

    def test_manual_verified_requires_pass_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report_path = root / "docs" / "compatibility-reports" / "obs.json"
            report_path.parent.mkdir(parents=True)
            report_path.write_text(
                json.dumps(self._report(result="PARTIAL")), encoding="utf-8"
            )

            errors = validator.validate_matrix_manual_reports(
                self._matrix(["docs/compatibility-reports/obs.json"]), root=root
            )

        self.assertTrue(
            any("manual_verified evidence must have result=PASS" in error for error in errors)
        )

    def test_manual_verified_rejects_failed_or_all_not_applicable_checks(self) -> None:
        failed = self._report(
            checks=[
                {"name": "publish accepted", "result": "PASS"},
                {"name": "disconnect and recover", "result": "FAIL"},
            ]
        )
        errors = validator.validate_report(failed, require_pass=True)
        self.assertTrue(
            any("must be PASS or NOT_APPLICABLE" in error for error in errors)
        )

        no_positive_check = self._report(
            checks=[{"name": "publish accepted", "result": "NOT_APPLICABLE"}]
        )
        errors = validator.validate_report(no_positive_check, require_pass=True)
        self.assertIn(
            "manual_verified evidence must include at least one PASS check", errors
        )

    def test_manual_report_must_match_entry_id_and_coverage(self) -> None:
        report = self._report(entry_id="other-entry", coverage="mobile.ios_a")

        errors = validator.validate_report(
            report,
            expected_entry_id="obs-real-device",
            expected_coverage="pc.obs",
            require_pass=True,
        )

        self.assertTrue(any("entry_id must match" in error for error in errors))
        self.assertTrue(any("coverage must match" in error for error in errors))

    def test_report_requires_timezone_aware_timestamp(self) -> None:
        errors = validator.validate_report(
            self._report(tested_at="2026-09-16T05:30:00")
        )

        self.assertIn(
            "tested_at must be a timezone-aware ISO 8601 timestamp", errors
        )

    def test_malformed_schema_and_enum_shapes_fail_closed(self) -> None:
        report = self._report(schema_version=True, result=["PASS"])
        report["checks"] = [{"name": "publish accepted", "result": ["PASS"]}]

        errors = validator.validate_report(report)

        self.assertIn("schema_version must be integer 1", errors)
        self.assertTrue(any(error.startswith("result must be one of") for error in errors))
        self.assertTrue(
            any(error.startswith("checks[0].result must be one of") for error in errors)
        )

    def test_report_rejects_sensitive_field_names_recursively(self) -> None:
        report = self._report()
        report["diagnostics"] = {
            "connection": {"stream_key": "AUDIT_DUMMY_SECRET"}
        }

        errors = validator.validate_report(report)

        self.assertTrue(
            any(
                "sensitive field name is not allowed in evidence: "
                "diagnostics.connection.stream_key" in error
                for error in errors
            )
        )

    def test_report_rejects_normalized_sensitive_field_name_variants(self) -> None:
        report = self._report()
        report["diagnostics"] = {
            "clientSecret": "AUDIT_DUMMY_SECRET",
            "access-token": "AUDIT_DUMMY_TOKEN",
        }

        errors = validator.validate_report(report)

        self.assertTrue(any("diagnostics.clientSecret" in error for error in errors))
        self.assertTrue(any("diagnostics.access-token" in error for error in errors))

    def test_report_rejects_credential_bearing_url_userinfo(self) -> None:
        report = self._report(
            notes=(
                "Observed endpoint "
                "rtmps://publisher:AUDIT_DUMMY_SECRET@example.invalid/live during QA"
            )
        )

        errors = validator.validate_report(report)

        self.assertIn(
            "credential-bearing URL is not allowed in evidence: notes", errors
        )
        self.assertTrue(all("AUDIT_DUMMY_SECRET" not in error for error in errors))

    def test_report_rejects_percent_encoded_sensitive_query_name(self) -> None:
        report = self._report()
        report["diagnostics"] = {
            "endpoint": (
                "srt://example.invalid:9000?Pass%70hrase=AUDIT_DUMMY_SECRET"
            )
        }

        errors = validator.validate_report(report)

        self.assertIn(
            "credential-bearing URL is not allowed in evidence: diagnostics.endpoint",
            errors,
        )
        self.assertTrue(all("AUDIT_DUMMY_SECRET" not in error for error in errors))

    def test_report_allows_non_credential_url(self) -> None:
        report = self._report(
            notes="Reference: https://example.invalid/docs?profile=1080p30&transport=rtmps"
        )

        self.assertEqual(validator.validate_report(report), [])

    def test_manual_evidence_must_be_json_under_report_directory(self) -> None:
        errors = validator.validate_matrix_manual_reports(
            self._matrix(["docs/compatibility-reports/obs.md"]), root=ROOT
        )
        self.assertTrue(any("must be a JSON report" in error for error in errors))

        errors = validator.validate_matrix_manual_reports(
            self._matrix(["docs/compatibility-reports/../obs.json"]), root=ROOT
        )
        self.assertTrue(any("unsafe manual evidence path" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
