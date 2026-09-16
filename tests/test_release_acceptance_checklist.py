from __future__ import annotations

import copy
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validate-release-acceptance-checklist.py"
CHECKLIST = ROOT / "docs" / "release-acceptance-checklist.json"

_spec = importlib.util.spec_from_file_location("release_acceptance_checklist", SCRIPT)
assert _spec is not None and _spec.loader is not None
module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(module)


class ReleaseAcceptanceChecklistTests(unittest.TestCase):
    def _canonical(self) -> dict[str, object]:
        return module.load_checklist(CHECKLIST)

    def test_repository_checklist_is_valid_and_not_ready(self) -> None:
        payload = self._canonical()
        module.validate_checklist(payload)
        self.assertFalse(payload["qa_acceptance_ready"])

    def test_duplicate_item_id_is_rejected(self) -> None:
        payload = self._canonical()
        duplicate = copy.deepcopy(payload["items"][0])
        payload["items"].append(duplicate)
        with self.assertRaisesRegex(module.ChecklistValidationError, "item ids"):
            module.validate_checklist(payload)

    def test_missing_required_item_is_rejected(self) -> None:
        payload = self._canonical()
        payload["items"] = [
            item
            for item in payload["items"]
            if item["id"] != "six-hour-soak"
        ]
        with self.assertRaisesRegex(module.ChecklistValidationError, "missing required"):
            module.validate_checklist(payload)

    def test_qa_acceptance_ready_cannot_overclaim_pending_items(self) -> None:
        payload = self._canonical()
        payload["qa_acceptance_ready"] = True
        with self.assertRaisesRegex(module.ChecklistValidationError, "qa_acceptance_ready"):
            module.validate_checklist(payload)

    def test_satisfied_item_requires_evidence(self) -> None:
        payload = self._canonical()
        item = next(
            entry for entry in payload["items"] if entry["id"] == "obs-compatibility"
        )
        item["status"] = "satisfied"
        with self.assertRaisesRegex(module.ChecklistValidationError, "require repository evidence"):
            module.validate_checklist(payload)

    def test_missing_evidence_path_is_rejected(self) -> None:
        payload = self._canonical()
        item = next(
            entry for entry in payload["items"] if entry["id"] == "obs-compatibility"
        )
        item["evidence"] = ["docs/does-not-exist-release-proof.json"]
        with self.assertRaisesRegex(module.ChecklistValidationError, "missing or unsafe"):
            module.validate_checklist(payload)

    def test_path_traversal_evidence_is_rejected(self) -> None:
        payload = self._canonical()
        item = next(
            entry for entry in payload["items"] if entry["id"] == "obs-compatibility"
        )
        item["evidence"] = ["../outside-proof.json"]
        with self.assertRaisesRegex(module.ChecklistValidationError, "inside the repository"):
            module.validate_checklist(payload)

    def test_unknown_status_is_rejected(self) -> None:
        payload = self._canonical()
        payload["items"][0]["status"] = "green"
        with self.assertRaisesRegex(module.ChecklistValidationError, "unsupported status"):
            module.validate_checklist(payload)

    def test_non_string_status_is_rejected_without_traceback(self) -> None:
        payload = self._canonical()
        payload["items"][0]["status"] = ["satisfied"]
        with self.assertRaisesRegex(module.ChecklistValidationError, "unsupported status"):
            module.validate_checklist(payload)

    def test_float_schema_version_is_rejected(self) -> None:
        payload = self._canonical()
        payload["schema_version"] = 1.0
        with self.assertRaisesRegex(module.ChecklistValidationError, "schema_version"):
            module.validate_checklist(payload)

    def test_duplicate_json_key_is_rejected(self) -> None:
        fd, name = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        path = Path(name)
        self.addCleanup(path.unlink, missing_ok=True)
        path.write_text(
            '{"schema_version":1,"schema_version":1,"qa_acceptance_ready":false,'
            '"policy":"x","items":[]}',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(module.ChecklistValidationError, "duplicate JSON key"):
            module.load_checklist(path)


if __name__ == "__main__":
    unittest.main()
