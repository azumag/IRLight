from __future__ import annotations

import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout


ROOT = pathlib.Path(__file__).resolve().parents[1]
VALIDATOR_PATH = ROOT / "scripts" / "validate-node-capacity-load-plan.py"
RENDERER_PATH = ROOT / "scripts" / "render-node-capacity-load-plan.py"


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


VALIDATOR = _load("node_capacity_load_plan_validator_test", VALIDATOR_PATH)
RENDERER = _load("node_capacity_load_plan_renderer_test", RENDERER_PATH)


class NodeCapacityLoadPlanValidationTests(unittest.TestCase):
    def _write_plan(self, directory: pathlib.Path, value: object) -> pathlib.Path:
        path = directory / "plan.json"
        path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        return path

    def test_renderer_output_validates_and_summary_is_stable(self) -> None:
        plan = RENDERER.build_plan("mixed-profile-v1", [16])
        summary = VALIDATOR.validate_plan(plan)
        self.assertEqual(summary["schema_version"], 1)
        self.assertEqual(summary["profile_label"], "mixed-profile-v1")
        self.assertEqual(summary["session_counts"], [1, 2, 4, 8, 16])
        self.assertEqual(
            summary["scenario_ids"],
            ["normal-input", "all-holding", "reconnect-storm", "asset-prefetch", "api-dashboard"],
        )

    def test_tampered_scenario_missing_baseline_and_typed_values_fail_closed(self) -> None:
        plan = RENDERER.build_plan("720p30 3Mbps")
        plan["scenarios"][0]["description"] = "changed"
        with self.assertRaisesRegex(VALIDATOR.PlanValidationError, "canonical renderer contract"):
            VALIDATOR.validate_plan(plan)

        plan = RENDERER.build_plan("720p30 3Mbps")
        plan["session_counts"] = [2, 4, 8]
        for scenario in plan["scenarios"]:
            scenario["session_counts"] = [2, 4, 8]
        with self.assertRaisesRegex(VALIDATOR.PlanValidationError, "canonical renderer contract"):
            VALIDATOR.validate_plan(plan)

        plan = RENDERER.build_plan("720p30 3Mbps")
        plan["schema_version"] = True
        with self.assertRaisesRegex(VALIDATOR.PlanValidationError, "canonical renderer contract"):
            VALIDATOR.validate_plan(plan)

        plan = RENDERER.build_plan("720p30 3Mbps")
        plan["scenarios"][0]["session_counts"][0] = True
        with self.assertRaisesRegex(VALIDATOR.PlanValidationError, "canonical renderer contract"):
            VALIDATOR.validate_plan(plan)

    def test_unexpected_field_and_noncanonical_order_fail_closed(self) -> None:
        plan = RENDERER.build_plan("1080p30 5Mbps", [16])
        plan["extra"] = True
        with self.assertRaises(VALIDATOR.PlanValidationError):
            VALIDATOR.validate_plan(plan)

        plan = RENDERER.build_plan("1080p30 5Mbps", [16])
        plan["session_counts"] = [1, 4, 2, 8, 16]
        with self.assertRaises(VALIDATOR.PlanValidationError):
            VALIDATOR.validate_plan(plan)

    def test_load_rejects_duplicate_key_symlink_and_oversize_input(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            duplicate = tmp / "duplicate.json"
            duplicate.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
            with self.assertRaisesRegex(VALIDATOR.PlanValidationError, "duplicate JSON key"):
                VALIDATOR.load_plan(duplicate)

            target = self._write_plan(tmp, RENDERER.build_plan("profile"))
            link = tmp / "plan-link.json"
            try:
                link.symlink_to(target.name)
            except (OSError, NotImplementedError):
                pass
            else:
                with self.assertRaisesRegex(VALIDATOR.PlanValidationError, "regular file"):
                    VALIDATOR.load_plan(link)

            oversized = tmp / "oversized.json"
            oversized.write_bytes(b" " * (VALIDATOR.MAX_PLAN_BYTES + 1))
            with self.assertRaisesRegex(VALIDATOR.PlanValidationError, "maximum size"):
                VALIDATOR.load_plan(oversized)

    def test_invalid_utf8_and_non_object_root_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            invalid_utf8 = tmp / "invalid.json"
            invalid_utf8.write_bytes(b"\xff")
            with self.assertRaisesRegex(VALIDATOR.PlanValidationError, "valid UTF-8"):
                VALIDATOR.load_plan(invalid_utf8)

            array_root = tmp / "array.json"
            array_root.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(VALIDATOR.PlanValidationError, "root must be an object"):
                VALIDATOR.load_plan(array_root)

    def test_cli_json_summary_and_tamper_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            path = self._write_plan(tmp, RENDERER.build_plan("qa-mix-v1", [16, 32]))
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(VALIDATOR.main([str(path), "--json"]), 0)
            payload = json.loads(stdout.getvalue())
            self.assertTrue(payload["valid"])
            self.assertEqual(payload["session_counts"], [1, 2, 4, 8, 16, 32])

            tampered = RENDERER.build_plan("qa-mix-v1")
            tampered["execution"] = "execute"
            path.write_text(json.dumps(tampered), encoding="utf-8")
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                self.assertEqual(VALIDATOR.main([str(path)]), 2)
            self.assertIn("validation failed", stderr.getvalue())

    def test_plain_text_success_does_not_echo_operator_profile_label(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            label = "profile-\x1b[31m-controlled"
            path = self._write_plan(tmp, RENDERER.build_plan(label))
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(VALIDATOR.main([str(path)]), 0)
            self.assertNotIn(label, stdout.getvalue())
            self.assertNotIn("\x1b", stdout.getvalue())

    def test_validator_has_no_load_execution_or_network_imports(self) -> None:
        source = VALIDATOR_PATH.read_text(encoding="utf-8")
        for forbidden in (
            "import subprocess",
            "from subprocess",
            "import socket",
            "from socket",
            "import requests",
            "import urllib",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
