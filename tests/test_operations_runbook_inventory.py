from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNBOOK_INDEX = "docs/operations/README.md"

REQUIRED_RUNBOOKS = {
    "media node heartbeat": "docs/operations/media-node-heartbeat-stopped.md",
    "session process crash loop": "docs/operations/session-process-crash-loop.md",
    "widespread egress failure": "docs/operations/egress-widespread-failure.md",
    "ingest unavailable": "docs/operations/ingest-connectivity-failure.md",
    "control plane unavailable": "docs/operations/control-plane-unavailable.md",
    "database or redis unavailable": "docs/operations/datastore-unavailable.md",
    "object storage unavailable": "docs/operations/object-storage-unavailable.md",
    "tls certificate update failure": "docs/operations/rtmps-certificate-update-failure.md",
    "capacity exhausted": "docs/operations/session-capacity-exhaustion.md",
    "secret exposure suspected": "docs/operations/secret-exposure-suspected.md",
    "billing webhook stalled": "docs/operations/billing-webhook-stalled.md",
    "emergency abuse stop": "docs/operations/emergency-abuse-stop.md",
}

NEW_RUNBOOKS = {
    "docs/operations/datastore-unavailable.md",
    "docs/operations/object-storage-unavailable.md",
    "docs/operations/billing-webhook-stalled.md",
    "docs/operations/emergency-abuse-stop.md",
}


class OperationsRunbookInventoryTests(unittest.TestCase):
    def test_issue_11_required_runbooks_exist(self) -> None:
        for scenario, relative_path in REQUIRED_RUNBOOKS.items():
            with self.subTest(scenario=scenario):
                path = REPO_ROOT / relative_path
                self.assertTrue(path.is_file(), f"missing runbook: {relative_path}")
                text = path.read_text(encoding="utf-8")
                self.assertTrue(text.startswith("# "), f"runbook must start with an H1: {relative_path}")
                self.assertGreater(len(text.strip()), 200, f"runbook is unexpectedly empty: {relative_path}")

    def test_issue_11_required_runbooks_are_linked_from_index(self) -> None:
        index_path = REPO_ROOT / RUNBOOK_INDEX
        self.assertTrue(index_path.is_file(), f"missing runbook index: {RUNBOOK_INDEX}")
        text = index_path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# "), "runbook index must start with an H1")
        self.assertIn("## 共通安全原則", text)
        self.assertIn("## Issue #11 必須 runbook", text)

        for scenario, relative_path in REQUIRED_RUNBOOKS.items():
            with self.subTest(scenario=scenario):
                link_target = Path(relative_path).name
                link_marker = f"]({link_target})"
                self.assertEqual(
                    text.count(link_marker),
                    1,
                    f"runbook index must link exactly once to {relative_path}",
                )

    def test_new_runbooks_have_actionable_lifecycle_sections(self) -> None:
        for relative_path in NEW_RUNBOOKS:
            with self.subTest(path=relative_path):
                text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
                self.assertIn("## 検知", text)
                self.assertIn("## 影響判定", text)
                self.assertIn("## 事後作業", text)


if __name__ == "__main__":
    unittest.main()
