from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
ASSEMBLER_PATH = ROOT / "scripts" / "assemble-node-capacity-planned-report.py"
PLAN_VALIDATOR_PATH = ROOT / "scripts" / "validate-node-capacity-load-plan.py"


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ASSEMBLER = _load("node_capacity_run_manifest_schema_test", ASSEMBLER_PATH)
PLAN_VALIDATOR = _load("node_capacity_run_manifest_schema_plan_validator", PLAN_VALIDATOR_PATH)


def _manifest() -> dict[str, object]:
    return {
        "schema_version": 1,
        "plan_sha256": "a" * 64,
        "profile_label": "720p30 3Mbps",
        "scenario_id": "normal-input",
        "node_profile": "c3.large-like",
        "software_revision": "c" * 40,
        "failure_policy": "continue",
        "planned_load_levels": [1, 2, 4, 8],
        "tested_load_levels": [1, 2, 4, 8],
        "boundary_found": True,
        "completed_plan": True,
        "trials_sha256": "b" * 64,
    }


class NodeCapacityRunManifestSchemaTests(unittest.TestCase):
    def _load_value(self, value: dict[str, object]) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            path = pathlib.Path(raw_directory) / "run.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            ASSEMBLER._load_run_manifest(path, PLAN_VALIDATOR)

    def test_float_schema_version_is_rejected(self) -> None:
        value = _manifest()
        value["schema_version"] = 1.0
        with self.assertRaisesRegex(ASSEMBLER.PlannedReportError, "schema version"):
            self._load_value(value)

    def test_unhashable_failure_policy_is_rejected_without_traceback(self) -> None:
        value = _manifest()
        value["failure_policy"] = []
        with self.assertRaisesRegex(ASSEMBLER.PlannedReportError, "failure policy"):
            self._load_value(value)

    def test_boolean_load_level_is_rejected(self) -> None:
        value = _manifest()
        value["tested_load_levels"] = [1, 2, True, 8]
        with self.assertRaisesRegex(ASSEMBLER.PlannedReportError, "tested load levels"):
            self._load_value(value)

    def test_blank_node_profile_is_rejected(self) -> None:
        value = _manifest()
        value["node_profile"] = "   "
        with self.assertRaisesRegex(ASSEMBLER.PlannedReportError, "node profile"):
            self._load_value(value)

    def test_noncanonical_software_revision_is_rejected(self) -> None:
        value = _manifest()
        value["software_revision"] = "C" * 40
        with self.assertRaisesRegex(ASSEMBLER.PlannedReportError, "software revision"):
            self._load_value(value)


if __name__ == "__main__":
    unittest.main()
