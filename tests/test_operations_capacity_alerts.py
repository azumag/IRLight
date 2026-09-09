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
MODULE_PATH = CONTROL_API_ROOT / "operations_capacity_alerts.py"
CATALOG_PATH = REPO_ROOT / "config" / "operations-alert-catalog.json"

if str(CONTROL_API_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROL_API_ROOT))

spec = importlib.util.spec_from_file_location("operations_capacity_alerts", MODULE_PATH)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def observation(maximum: object, active: object, reserved: object) -> str:
    return json.dumps(
        {
            "max_sessions": maximum,
            "active_sessions": active,
            "reserved_sessions": reserved,
        },
        separators=(",", ":"),
    ) + "\n"


class OperationsCapacityAlertTests(unittest.TestCase):
    def load_catalog(self) -> dict[str, object]:
        return module.load_catalog(CATALOG_PATH, repo_root=REPO_ROOT)

    def test_strictly_above_80_percent_matches_but_exact_boundary_does_not(self) -> None:
        result = module.evaluate_lines(
            [
                observation(10, 8, 0),
                observation(10, 8, 1),
                observation(5, 3, 1),
                observation(3, 2, 1),
            ],
            self.load_catalog(),
        )

        self.assertEqual(result["status"], "MATCHED")
        self.assertEqual(result["records"], 4)
        self.assertEqual(result["matched_alerts"], {"MEDIA_NODE_CAPACITY_HIGH": 2})
        self.assertEqual(result["unmatched_records"], 2)
        self.assertEqual(result["violations"], {})

    def test_overcommitted_capacity_is_a_match_not_invalid_data(self) -> None:
        result = module.evaluate_lines(
            [observation(4, 4, 2)],
            self.load_catalog(),
        )
        self.assertEqual(result["status"], "MATCHED")
        self.assertEqual(result["matched_alerts"], {"MEDIA_NODE_CAPACITY_HIGH": 1})

    def test_zero_or_invalid_capacity_values_fail_closed(self) -> None:
        bad_cases = (
            observation(0, 0, 0),
            observation(10, -1, 0),
            observation(10, True, 0),
            observation(10.0, 8, 1),
            observation("10", 8, 1),
        )
        for line in bad_cases:
            with self.subTest(line=line):
                result = module.evaluate_lines([line], self.load_catalog())
                self.assertEqual(result["status"], "INVALID")
                self.assertEqual(result["violations"], {"INVALID_CAPACITY_VALUE": 1})
                self.assertEqual(result["matched_alerts"], {})

    def test_extra_identifier_or_payload_fields_are_rejected(self) -> None:
        line = json.dumps(
            {
                "max_sessions": 10,
                "active_sessions": 9,
                "reserved_sessions": 0,
                "node_id": "node-sensitive-context",
            }
        ) + "\n"
        result = module.evaluate_lines([line], self.load_catalog())
        rendered = json.dumps(result, sort_keys=True)

        self.assertEqual(result["status"], "INVALID")
        self.assertEqual(result["violations"], {"INVALID_RECORD_FIELDS": 1})
        self.assertNotIn("node-sensitive-context", rendered)

    def test_invalid_batch_suppresses_partial_matches(self) -> None:
        result = module.evaluate_lines(
            [observation(10, 9, 0), observation(0, 0, 0)],
            self.load_catalog(),
        )

        self.assertEqual(result["status"], "INVALID")
        self.assertEqual(result["records"], 2)
        self.assertEqual(result["matched_alerts"], {})
        self.assertEqual(result["unmatched_records"], 0)

    def test_duplicate_keys_nonfinite_and_invalid_utf8_fail_closed(self) -> None:
        duplicate = (
            '{"max_sessions":10,"max_sessions":12,'
            '"active_sessions":9,"reserved_sessions":0}\n'
        )
        nonfinite = (
            '{"max_sessions":10,"active_sessions":NaN,"reserved_sessions":0}\n'
        )
        duplicate_result = module.evaluate_lines([duplicate], self.load_catalog())
        nonfinite_result = module.evaluate_lines([nonfinite], self.load_catalog())
        utf8_result = module.evaluate_lines(
            list(module._iter_bounded_byte_lines(io.BytesIO(b"\xff\xfe\n"))),
            self.load_catalog(),
        )

        self.assertEqual(duplicate_result["violations"], {"DUPLICATE_JSON_KEY": 1})
        self.assertEqual(nonfinite_result["violations"], {"NONFINITE_NUMBER": 1})
        self.assertEqual(utf8_result["violations"], {"INVALID_JSON": 1})

    def test_oversized_raw_record_is_drained_as_one_invalid_record(self) -> None:
        raw = b"x" * (module._MAX_RECORD_BYTES + 100) + b"\n"
        lines = list(module._iter_bounded_byte_lines(io.BytesIO(raw)))
        self.assertEqual(len(lines), 1)

        result = module.evaluate_lines(lines, self.load_catalog())
        self.assertEqual(result["status"], "INVALID")
        self.assertEqual(result["records"], 1)
        self.assertEqual(result["violations"], {"RECORD_TOO_LARGE": 1})

    def test_capacity_evaluator_rejects_catalog_contract_drift(self) -> None:
        for field, value in (
            ("signal", "sessions.capacity_ratio"),
            ("severity", "critical"),
            ("runbook", "docs/operations/session-capacity-exhaustion.md"),
        ):
            with self.subTest(field=field):
                catalog = copy.deepcopy(self.load_catalog())
                alert = next(
                    candidate
                    for candidate in catalog["alerts"]
                    if candidate["id"] == "MEDIA_NODE_CAPACITY_HIGH"
                )
                alert[field] = value
                with self.assertRaises(module.OperationsCapacityAlertError):
                    module.evaluate_lines([observation(10, 9, 0)], catalog)

    def test_no_matches_is_a_successful_valid_result(self) -> None:
        result = module.evaluate_lines(
            [observation(10, 7, 1), observation(10, 0, 0)],
            self.load_catalog(),
        )
        self.assertEqual(result["status"], "NO_MATCHES")
        self.assertEqual(result["matched_alerts"], {})
        self.assertEqual(result["unmatched_records"], 2)
        self.assertEqual(module._exit_code(result["status"]), 0)


if __name__ == "__main__":
    unittest.main()
