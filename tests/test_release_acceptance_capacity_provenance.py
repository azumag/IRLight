from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validate-release-acceptance-checklist.py"
SPEC = importlib.util.spec_from_file_location(
    "release_acceptance_capacity_provenance_test", SCRIPT
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

EXISTING_EVIDENCE = "docs/release-acceptance-checklist.md"


class _CoverageError(ValueError):
    pass


class ReleaseAcceptanceCapacityProvenanceTests(unittest.TestCase):
    def _validator(self, *, provenance_bound: bool):
        return SimpleNamespace(
            CapacityCoverageManifestError=_CoverageError,
            validate_manifest_file=mock.Mock(
                return_value={
                    "valid": True,
                    "schema_version": 2 if provenance_bound else 1,
                    "provenance_bound": provenance_bound,
                }
            ),
        )

    def test_schema_v1_coverage_cannot_satisfy_release_capacity_gate(self) -> None:
        validator = self._validator(provenance_bound=False)
        with mock.patch.object(
            MODULE,
            "_load_capacity_coverage_manifest_validator",
            return_value=validator,
        ):
            with self.assertRaisesRegex(
                MODULE.ChecklistValidationError,
                "provenance-bound Node capacity coverage manifest",
            ):
                MODULE._validate_node_capacity_evidence([EXISTING_EVIDENCE])

    def test_schema_v2_provenance_coverage_can_satisfy_release_capacity_gate(self) -> None:
        validator = self._validator(provenance_bound=True)
        with mock.patch.object(
            MODULE,
            "_load_capacity_coverage_manifest_validator",
            return_value=validator,
        ):
            MODULE._validate_node_capacity_evidence([EXISTING_EVIDENCE])

        validator.validate_manifest_file.assert_called_once_with(
            ROOT / EXISTING_EVIDENCE,
            repo_root=ROOT,
        )


if __name__ == "__main__":
    unittest.main()
