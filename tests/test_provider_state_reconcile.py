from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from provider.conoha import ManagedResource  # noqa: E402
from provider_state_reconcile_cli import (  # noqa: E402
    load_session_snapshot,
    reconcile_provider_resources,
)
from session_store import new_session  # noqa: E402
from state_readiness import StateReadinessError  # noqa: E402


class ProviderStateReconcileTest(unittest.TestCase):
    def _session(self) -> dict[str, object]:
        session = new_session(user_id="deadbeef")
        session["provider_volume_id"] = "volume-1"
        session["provider_server_id"] = "server-1"
        return session

    def _resources(self, session_id: str) -> list[ManagedResource]:
        return [
            ManagedResource(
                kind="volume",
                provider_id="volume-1",
                session_id=session_id,
                user_id="deadbeef",
                created_at=1.0,
                delete_after=None,
                details={"token": "AUDIT_DUMMY_SECRET"},
            ),
            ManagedResource(
                kind="server",
                provider_id="server-1",
                session_id=session_id,
                user_id="deadbeef",
                created_at=1.0,
                delete_after=None,
                details={"password": "AUDIT_DUMMY_SECRET"},
            ),
        ]

    def test_matching_inventory_reports_no_findings_or_details(self) -> None:
        session = self._session()
        session_id = str(session["session_id"])

        report = reconcile_provider_resources(
            {session_id: session}, self._resources(session_id)
        )

        self.assertEqual(report["status"], "MATCH")
        self.assertEqual(report["findings"], [])
        serialized = json.dumps(report, sort_keys=True)
        self.assertNotIn("AUDIT_DUMMY_SECRET", serialized)
        self.assertNotIn("details", serialized)

    def test_missing_state_reference_and_provider_orphan_require_review(self) -> None:
        session = self._session()
        session_id = str(session["session_id"])
        resources = [self._resources(session_id)[0]]
        resources.append(
            ManagedResource(
                kind="server",
                provider_id="orphan-server",
                session_id="00000000-0000-4000-8000-000000000001",
                user_id="deadbeef",
                created_at=1.0,
                delete_after=None,
            )
        )

        report = reconcile_provider_resources({session_id: session}, resources)
        reasons = {item["reason_code"] for item in report["findings"]}

        self.assertEqual(report["status"], "REVIEW_REQUIRED")
        self.assertIn("STATE_PROVIDER_RESOURCE_MISSING", reasons)
        self.assertIn("PROVIDER_RESOURCE_SESSION_MISSING", reasons)

    def test_wrong_resource_owner_and_unreferenced_resource_are_reported(self) -> None:
        session = self._session()
        session_id = str(session["session_id"])
        resources = [
            ManagedResource(
                kind="volume",
                provider_id="volume-1",
                session_id=session_id,
                user_id="cafebabe",
                created_at=1.0,
                delete_after=None,
            ),
            ManagedResource(
                kind="server",
                provider_id="different-server",
                session_id=session_id,
                user_id="deadbeef",
                created_at=1.0,
                delete_after=None,
            ),
        ]

        report = reconcile_provider_resources({session_id: session}, resources)
        reasons = {item["reason_code"] for item in report["findings"]}

        self.assertIn("PROVIDER_USER_OWNERSHIP_MISMATCH", reasons)
        self.assertIn("STATE_PROVIDER_RESOURCE_MISSING", reasons)
        self.assertIn("PROVIDER_RESOURCE_NOT_REFERENCED_BY_SESSION", reasons)

    def test_invalid_state_provider_reference_is_not_coerced(self) -> None:
        session = self._session()
        session_id = str(session["session_id"])
        session["provider_volume_id"] = 123

        report = reconcile_provider_resources({session_id: session}, [])

        self.assertIn(
            "STATE_PROVIDER_REFERENCE_INVALID",
            {item["reason_code"] for item in report["findings"]},
        )

    def test_load_snapshot_requires_existing_marker_without_creating_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            session = self._session()
            session_id = str(session["session_id"])
            state_path = state_dir / "sessions.json"
            state_path.write_text(
                json.dumps(
                    {"sessions": {session_id: session}, "orphan_cleanup_leases": {}}
                ),
                encoding="utf-8",
            )
            before = sorted(path.name for path in state_dir.iterdir())

            with self.assertRaises(StateReadinessError):
                load_session_snapshot(state_dir)

            after = sorted(path.name for path in state_dir.iterdir())
            self.assertEqual(after, before)
            self.assertFalse((state_dir / ".sessions.json.initialized").exists())

    def test_load_snapshot_is_read_only_for_valid_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            session = self._session()
            session_id = str(session["session_id"])
            state_path = state_dir / "sessions.json"
            marker = state_dir / ".sessions.json.initialized"
            state_path.write_text(
                json.dumps(
                    {"sessions": {session_id: session}, "orphan_cleanup_leases": {}}
                ),
                encoding="utf-8",
            )
            marker.write_text("v1\n", encoding="utf-8")
            before_names = sorted(path.name for path in state_dir.iterdir())
            before_stats = {
                path.name: (path.stat().st_mtime_ns, path.stat().st_size)
                for path in state_dir.iterdir()
            }

            loaded = load_session_snapshot(state_dir)

            self.assertEqual(loaded[session_id]["provider_server_id"], "server-1")
            self.assertEqual(
                sorted(path.name for path in state_dir.iterdir()), before_names
            )
            after_stats = {
                path.name: (path.stat().st_mtime_ns, path.stat().st_size)
                for path in state_dir.iterdir()
            }
            self.assertEqual(after_stats, before_stats)
            self.assertFalse((state_dir / ".sessions.lock").exists())


if __name__ == "__main__":
    unittest.main()
