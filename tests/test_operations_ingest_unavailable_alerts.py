from __future__ import annotations

import copy
import importlib.util
import io
import json
import math
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CONTROL_API_ROOT = REPO_ROOT / "apps" / "control-api"
MODULE_PATH = CONTROL_API_ROOT / "operations_ingest_unavailable_alerts.py"
CATALOG_PATH = REPO_ROOT / "config" / "operations-alert-catalog.json"

if str(CONTROL_API_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROL_API_ROOT))

spec = importlib.util.spec_from_file_location("operations_ingest_unavailable_alerts", MODULE_PATH)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def observation(rate: object) -> str:
    return json.dumps({"connectivity_failure_rate": rate}, separators=(",", ":")) + "\n"


class OperationsIngestUnavailableAlertTests(unittest.TestCase):
    def load_catalog(self) -> dict[str, object]:
        return module.load_catalog(CATALOG_PATH, repo_root=REPO_ROOT)

    def test_explicit_threshold_matches_only_strictly_above_boundary(self) -> None:
        result = module.evaluate_lines(
            [observation(0.09), observation(0.10), observation(0.11)],
            self.load_catalog(),
            threshold=0.10,
        )

        self.assertEqual(result["status"], "MATCHED")
        self.assertEqual(result["records"], 3)
        self.assertEqual(result["matched_alerts"], {"INGEST_UNAVAILABLE": 1})
        self.assertEqual(result["unmatched_records"], 2)
        self.assertEqual(result["violations"], {})

    def test_zero_threshold_does_not_classify_zero_rate_as_unavailable(self) -> None:
        result = module.evaluate_lines(
            [observation(0), observation(0.01)],
            self.load_catalog(),
            threshold=0,
        )
        self.assertEqual(result["status"], "MATCHED")
        self.assertEqual(result["matched_alerts"], {"INGEST_UNAVAILABLE": 1})
        self.assertEqual(result["unmatched_records"], 1)

    def test_repository_does_not_supply_a_production_threshold(self) -> None:
        with self.assertRaises(module.OperationsIngestUnavailableAlertError):
            module._validated_threshold(-0.1)
        with self.assertRaises(module.OperationsIngestUnavailableAlertError):
            module._validated_threshold(math.inf)
        with self.assertRaises(module.OperationsIngestUnavailableAlertError):
            module._validated_threshold(True)

    def test_invalid_connectivity_failure_rate_values_fail_closed(self) -> None:
        bad_cases = (
            observation(-0.1),
            observation(True),
            observation("0.1"),
            observation(None),
            '{"connectivity_failure_rate":1e309}\n',
        )
        for line in bad_cases:
            with self.subTest(line=line):
                result = module.evaluate_lines(
                    [line], self.load_catalog(), threshold=0.1
                )
                self.assertEqual(result["status"], "INVALID")
                self.assertEqual(
                    result["violations"],
                    {"INVALID_CONNECTIVITY_FAILURE_RATE": 1},
                )
                self.assertEqual(result["matched_alerts"], {})

    def test_extra_identifier_or_payload_fields_are_rejected(self) -> None:
        line = json.dumps(
            {
                "connectivity_failure_rate": 0.2,
                "region": "SENSITIVE_CONTEXT",
            }
        ) + "\n"
        result = module.evaluate_lines([line], self.load_catalog(), threshold=0.1)
        rendered = json.dumps(result, sort_keys=True)

        self.assertEqual(result["status"], "INVALID")
        self.assertEqual(result["violations"], {"INVALID_RECORD_FIELDS": 1})
        self.assertNotIn("SENSITIVE_CONTEXT", rendered)

    def test_invalid_batch_suppresses_partial_matches(self) -> None:
        result = module.evaluate_lines(
            [observation(0.2), observation(-1)],
            self.load_catalog(),
            threshold=0.1,
        )

        self.assertEqual(result["status"], "INVALID")
        self.assertEqual(result["records"], 2)
        self.assertEqual(result["matched_alerts"], {})
        self.assertEqual(result["unmatched_records"], 0)

    def test_duplicate_keys_nonfinite_and_invalid_utf8_fail_closed(self) -> None:
        duplicate = (
            '{"connectivity_failure_rate":0.1,"connectivity_failure_rate":0.2}\n'
        )
        nonfinite = '{"connectivity_failure_rate":NaN}\n'
        duplicate_result = module.evaluate_lines(
            [duplicate], self.load_catalog(), threshold=0.1
        )
        nonfinite_result = module.evaluate_lines(
            [nonfinite], self.load_catalog(), threshold=0.1
        )
        utf8_result = module.evaluate_lines(
            list(module._iter_bounded_byte_lines(io.BytesIO(b"\xff\xfe\n"))),
            self.load_catalog(),
            threshold=0.1,
        )

        self.assertEqual(duplicate_result["violations"], {"DUPLICATE_JSON_KEY": 1})
        self.assertEqual(nonfinite_result["violations"], {"NONFINITE_NUMBER": 1})
        self.assertEqual(utf8_result["violations"], {"INVALID_JSON": 1})

    def test_oversized_raw_record_is_drained_as_one_invalid_record(self) -> None:
        raw = b"x" * (module._MAX_RECORD_BYTES + 100) + b"\n"
        lines = list(module._iter_bounded_byte_lines(io.BytesIO(raw)))
        self.assertEqual(len(lines), 1)

        result = module.evaluate_lines(lines, self.load_catalog(), threshold=0.1)
        self.assertEqual(result["status"], "INVALID")
        self.assertEqual(result["records"], 1)
        self.assertEqual(result["violations"], {"RECORD_TOO_LARGE": 1})

    def test_evaluator_rejects_catalog_contract_drift(self) -> None:
        for field, value in (
            ("signal", "egress.failure_rate"),
            ("severity", "warning"),
            ("runbook", "docs/operations/egress-widespread-failure.md"),
        ):
            with self.subTest(field=field):
                catalog = copy.deepcopy(self.load_catalog())
                alert = next(
                    candidate
                    for candidate in catalog["alerts"]
                    if candidate["id"] == "INGEST_UNAVAILABLE"
                )
                alert[field] = value
                with self.assertRaises(module.OperationsIngestUnavailableAlertError):
                    module.evaluate_lines([observation(0.2)], catalog, threshold=0.1)

    def test_trigger_contract_drift_is_rejected(self) -> None:
        catalog = copy.deepcopy(self.load_catalog())
        alert = next(
            candidate
            for candidate in catalog["alerts"]
            if candidate["id"] == "INGEST_UNAVAILABLE"
        )
        alert["trigger"] = {
            "mode": "threshold",
            "threshold_ref": "operations.egress_failure_surge",
        }
        with self.assertRaises(module.OperationsIngestUnavailableAlertError):
            module.evaluate_lines([observation(0.2)], catalog, threshold=0.1)

    def test_no_matches_is_a_successful_valid_result(self) -> None:
        result = module.evaluate_lines(
            [observation(0), observation(0.09), observation(0.10)],
            self.load_catalog(),
            threshold=0.10,
        )
        self.assertEqual(result["status"], "NO_MATCHES")
        self.assertEqual(result["matched_alerts"], {})
        self.assertEqual(result["unmatched_records"], 3)
        self.assertEqual(module._exit_code(result["status"]), 0)


if __name__ == "__main__":
    unittest.main()
