from __future__ import annotations

import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from entitlement_store import EntitlementStateError, EntitlementStore  # noqa: E402
from session_store import EntitlementExceeded, SessionStore  # noqa: E402


class EntitlementStoreTest(unittest.TestCase):
    def test_default_and_persisted_override(self) -> None:
        state_dir = tempfile.mkdtemp(prefix="irlight-entitlements-")
        previous = os.environ.get("IRLIGHT_DEFAULT_MAX_CONCURRENT_SESSIONS")
        os.environ["IRLIGHT_DEFAULT_MAX_CONCURRENT_SESSIONS"] = "2"
        try:
            store = EntitlementStore(state_dir)
            default = store.get("user-a")
            self.assertEqual(default["max_concurrent_sessions"], 2)
            self.assertEqual(default["plan"], "default")

            updated = store.set(
                "user-a", max_concurrent_sessions=3, plan="supporter"
            )
            self.assertEqual(updated["max_concurrent_sessions"], 3)

            reloaded = EntitlementStore(state_dir).get("user-a")
            self.assertEqual(reloaded["max_concurrent_sessions"], 3)
            self.assertEqual(reloaded["plan"], "supporter")
        finally:
            if previous is None:
                os.environ.pop("IRLIGHT_DEFAULT_MAX_CONCURRENT_SESSIONS", None)
            else:
                os.environ["IRLIGHT_DEFAULT_MAX_CONCURRENT_SESSIONS"] = previous

    def test_zero_limit_can_disable_session_creation(self) -> None:
        state_dir = tempfile.mkdtemp(prefix="irlight-entitlements-")
        entitlement = EntitlementStore(state_dir).set(
            "user-a", max_concurrent_sessions=0, plan="disabled"
        )
        self.assertEqual(entitlement["max_concurrent_sessions"], 0)

    def test_stale_store_cannot_overwrite_another_process_update(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            first = EntitlementStore(state_dir)
            second = EntitlementStore(state_dir)

            first.set("user-a", max_concurrent_sessions=2, plan="supporter")
            second.set("user-b", max_concurrent_sessions=3, plan="creator")

            reloaded = EntitlementStore(state_dir)
            self.assertEqual(reloaded.get("user-a")["max_concurrent_sessions"], 2)
            self.assertEqual(reloaded.get("user-b")["max_concurrent_sessions"], 3)

    def test_corrupt_state_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            Path(state_dir, "entitlements.json").write_text("[]", encoding="utf-8")
            with self.assertRaises(EntitlementStateError):
                EntitlementStore(state_dir)


class ConcurrentSessionLimitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = SessionStore(tempfile.mkdtemp(prefix="irlight-sessions-"))
        self.user_id = "deadbeef"
        self.entitlement_id = "user:deadbeef"

    def _reserve(
        self,
        session_id: str,
        *,
        user_id: str | None = None,
        max_concurrent_sessions: int = 1,
    ) -> dict[str, object]:
        return self.store.reserve_prepare_slot(
            session_id,
            user_id=user_id or self.user_id,
            environment="dev",
            entitlement_id=self.entitlement_id,
            max_concurrent_sessions=max_concurrent_sessions,
        )

    def test_zero_limit_rejects_prepare_slot(self) -> None:
        with self.assertRaises(EntitlementExceeded):
            self._reserve(str(uuid.uuid4()), max_concurrent_sessions=0)

    def test_reservation_itself_consumes_slot(self) -> None:
        first_id = str(uuid.uuid4())
        second_id = str(uuid.uuid4())
        first = self._reserve(first_id)
        self.assertEqual(first["status"], "STOPPED")
        self.assertTrue(first["entitlement_reserved"])

        with self.assertRaises(EntitlementExceeded):
            self._reserve(second_id)

    def test_finished_session_releases_slot(self) -> None:
        first_id = str(uuid.uuid4())
        second_id = str(uuid.uuid4())
        self._reserve(first_id)
        self.store.transition(first_id, "PROVISIONING")

        with self.assertRaises(EntitlementExceeded):
            self._reserve(second_id)

        self.store.transition(first_id, "BOOTSTRAPPING")
        self.store.transition(first_id, "READY_WAIT_INGEST")
        self.store.transition(first_id, "STOPPING")
        finished = self.store.transition(first_id, "FINISHED")
        self.assertFalse(finished["entitlement_reserved"])

        second = self._reserve(second_id)
        self.assertTrue(second["entitlement_reserved"])

    def test_failed_cleanup_keeps_slot_until_terminal(self) -> None:
        first_id = str(uuid.uuid4())
        second_id = str(uuid.uuid4())
        self._reserve(first_id)
        self.store.transition(first_id, "PROVISIONING")
        self.store.transition(first_id, "FAILED_CLEANUP", cleanup_pending=True)

        with self.assertRaises(EntitlementExceeded):
            self._reserve(second_id)

        failed = self.store.transition(first_id, "FAILED", cleanup_pending=False)
        self.assertFalse(failed["entitlement_reserved"])
        self._reserve(second_id)

    def test_limits_are_per_user(self) -> None:
        self._reserve(str(uuid.uuid4()), user_id="user-a")
        other = self._reserve(str(uuid.uuid4()), user_id="user-b")
        self.assertEqual(other["user_id"], "user-b")


if __name__ == "__main__":
    unittest.main()
