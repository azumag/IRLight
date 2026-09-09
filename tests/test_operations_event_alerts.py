from __future__ import annotations

import copy
import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CONTROL_API_ROOT = REPO_ROOT / "apps" / "control-api"
MODULE_PATH = CONTROL_API_ROOT / "operations_event_alerts.py"
CATALOG_PATH = REPO_ROOT / "config" / "operations-alert-catalog.json"

if str(CONTROL_API_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROL_API_ROOT))

spec = importlib.util.spec_from_file_location("operations_event_alerts", MODULE_PATH)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def structured_record(event_type: str, **extra: object) -> str:
    payload: dict[str, object] = {
        "timestamp": "2026-09-10T03:00:00+09:00",
        "level": "error",
        "service": "control-api",
        "event_type": event_type,
    }
    payload.update(extra)
    return json.dumps(payload, separators=(",", ":")) + "\n"


class OperationsEventAlertTests(unittest.TestCase):
    def load_catalog(self) -> dict[str, object]:
        return module.load_catalog(CATALOG_PATH, repo_root=REPO_ROOT)

    def test_event_alerts_match_real_catalog_and_threshold_alerts_do_not(self) -> None:
        result = module.evaluate_lines(
            [
                structured_record("CONTROL_PLANE_UNAVAILABLE"),
                structured_record("NODE_RESOURCE_EXHAUSTED"),
                structured_record("NODE_RESOURCE_EXHAUSTED"),
                structured_record("SESSION_CAPACITY_HIGH"),
                structured_record("UNRELATED_OPERATIONAL_EVENT"),
            ],
            self.load_catalog(),
        )

        self.assertEqual(result["status"], "MATCHED")
        self.assertEqual(
            result["matched_alerts"],
            {
                "CONTROL_PLANE_UNAVAILABLE": 1,
                "NODE_RESOURCE_EXHAUSTED": 2,
            },
        )
        self.assertEqual(result["unmatched_records"], 2)
        self.assertEqual(result["violations"], {})

    def test_output_never_contains_source_values_or_correlation_identifiers(self) -> None:
        dummy_secret = "AUDIT_DUMMY_SECRET_DO_NOT_LOG"
        dummy_session = "session-sensitive-context"
        result = module.evaluate_lines(
            [
                structured_record(
                    "SECRET_EXPOSURE_SUSPECTED",
                    token=dummy_secret,
                    session_id=dummy_session,
                    message="arbitrary producer text",
                )
            ],
            self.load_catalog(),
        )

        rendered = json.dumps(result, sort_keys=True)
        self.assertEqual(result["status"], "MATCHED")
        self.assertEqual(result["matched_alerts"], {"SECRET_EXPOSURE_SUSPECTED": 1})
        self.assertNotIn(dummy_secret, rendered)
        self.assertNotIn(dummy_session, rendered)
        self.assertNotIn("arbitrary producer text", rendered)

    def test_invalid_batch_suppresses_partial_match_results(self) -> None:
        result = module.evaluate_lines(
            [
                structured_record("CONTROL_PLANE_UNAVAILABLE"),
                '{"timestamp":"now","level":"error","service":"control-api"}\n',
            ],
            self.load_catalog(),
        )

        self.assertEqual(result["status"], "INVALID")
        self.assertEqual(result["matched_alerts"], {})
        self.assertEqual(result["unmatched_records"], 0)
        self.assertEqual(result["violations"], {"MISSING_REQUIRED_FIELD": 1})

    def test_duplicate_json_keys_and_nonfinite_numbers_fail_closed(self) -> None:
        cases = (
            '{"timestamp":"now","level":"error","service":"control-api",'
            '"event_type":"CONTROL_PLANE_UNAVAILABLE",'
            '"event_type":"NODE_RESOURCE_EXHAUSTED"}\n',
            '{"timestamp":"now","level":"error","service":"control-api",'
            '"event_type":"CONTROL_PLANE_UNAVAILABLE","value":NaN}\n',
        )
        expected = ("DUPLICATE_JSON_KEY", "NONFINITE_NUMBER")

        for line, reason in zip(cases, expected, strict=True):
            with self.subTest(reason=reason):
                result = module.evaluate_lines([line], self.load_catalog())
                self.assertEqual(result["status"], "INVALID")
                self.assertEqual(result["violations"], {reason: 1})

    def test_required_fields_must_be_nonempty_strings(self) -> None:
        for field, bad_value in (
            ("timestamp", ""),
            ("level", 5),
            ("service", None),
            ("event_type", []),
        ):
            with self.subTest(field=field):
                payload = {
                    "timestamp": "now",
                    "level": "error",
                    "service": "control-api",
                    "event_type": "CONTROL_PLANE_UNAVAILABLE",
                }
                payload[field] = bad_value
                result = module.evaluate_lines(
                    [json.dumps(payload) + "\n"], self.load_catalog()
                )
                self.assertEqual(result["status"], "INVALID")
                self.assertEqual(result["violations"], {"INVALID_REQUIRED_FIELD": 1})

    def test_oversized_raw_record_is_drained_as_one_invalid_record(self) -> None:
        raw = b"x" * (module._MAX_RECORD_BYTES + 10) + b"\n"
        lines = list(module._iter_bounded_byte_lines(io.BytesIO(raw)))
        self.assertEqual(len(lines), 1)

        result = module.evaluate_lines(lines, self.load_catalog())
        self.assertEqual(result["status"], "INVALID")
        self.assertEqual(result["records"], 1)
        self.assertEqual(result["violations"], {"RECORD_TOO_LARGE": 1})

    def test_invalid_utf8_is_rejected_without_echoing_input(self) -> None:
        lines = list(module._iter_bounded_byte_lines(io.BytesIO(b"\xff\xfe\n")))
        result = module.evaluate_lines(lines, self.load_catalog())
        self.assertEqual(result["status"], "INVALID")
        self.assertEqual(result["violations"], {"INVALID_JSON": 1})

    def test_excessive_nesting_is_rejected(self) -> None:
        nested: object = "leaf"
        for _ in range(module._MAX_NESTING_DEPTH + 2):
            nested = {"value": nested}
        result = module.evaluate_lines(
            [structured_record("CONTROL_PLANE_UNAVAILABLE", payload=nested)],
            self.load_catalog(),
        )
        self.assertEqual(result["status"], "INVALID")
        self.assertEqual(result["violations"], {"NESTING_TOO_DEEP": 1})

    def test_ambiguous_event_type_mapping_is_rejected(self) -> None:
        catalog = copy.deepcopy(self.load_catalog())
        first_event = next(
            alert
            for alert in catalog["alerts"]
            if alert["trigger"]["mode"] == "event"
        )
        duplicate = copy.deepcopy(first_event)
        duplicate["id"] = "SECOND_ALERT_FOR_SAME_EVENT"
        catalog["alerts"].append(duplicate)

        with self.assertRaises(module.OperationsEventAlertError):
            module._build_event_alert_index(catalog)

    def test_no_matches_is_a_successful_valid_result(self) -> None:
        result = module.evaluate_lines(
            [structured_record("UNRELATED_OPERATIONAL_EVENT")],
            self.load_catalog(),
        )
        self.assertEqual(result["status"], "NO_MATCHES")
        self.assertEqual(result["matched_alerts"], {})
        self.assertEqual(result["unmatched_records"], 1)
        self.assertEqual(module._exit_code(result["status"]), 0)


if __name__ == "__main__":
    unittest.main()
