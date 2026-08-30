from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from cryptography.fernet import Fernet


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from ingest_store import (  # noqa: E402
    IngestCredentialError,
    IngestCredentialStore,
)
from destination_secret_store import (  # noqa: E402
    DestinationSecretError,
    DestinationSecretStore,
)
from entitlement_store import EntitlementStateError, EntitlementStore  # noqa: E402
from session_store import SessionStateError, SessionStore  # noqa: E402


class SessionStoreSafetyTest(unittest.TestCase):
    def test_stale_store_cannot_overwrite_another_process_update(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            first = SessionStore(state_dir)
            session = first.create(user_id="deadbeef", environment="dev")
            second = SessionStore(state_dir)

            second.update(str(session["session_id"]), writer_two=True)
            first.create(user_id="cafebabe", environment="dev")

            persisted = SessionStore(state_dir).get(str(session["session_id"]))
            self.assertIsNotNone(persisted)
            assert persisted is not None
            self.assertTrue(persisted["writer_two"])

    def test_corrupt_state_is_not_treated_as_an_empty_store(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            Path(state_dir, "sessions.json").write_text("{broken", encoding="utf-8")
            with self.assertRaises(SessionStateError):
                SessionStore(state_dir)

    def test_initialized_state_deletion_cannot_reestablish_authority(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            store = SessionStore(state_dir)
            store.create(user_id="user-1", environment="dev")
            Path(state_dir, "sessions.json").unlink()

            with self.assertRaises(SessionStateError):
                store.create(user_id="user-2", environment="dev")
            with self.assertRaises(SessionStateError):
                SessionStore(state_dir)


class IngestCredentialStoreSafetyTest(unittest.TestCase):
    def test_stale_store_cannot_resurrect_revoked_credential(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            first = IngestCredentialStore(state_dir)
            record, secret = first.issue(
                session_id="session-1",
                user_id="user-1",
                scope="INGEST",
                protocols=["rtmp"],
            )
            second = IngestCredentialStore(state_dir)

            second.revoke(str(record["id"]), user_id="user-1")
            first.issue(
                session_id="session-2",
                user_id="user-2",
                scope="INGEST",
                protocols=["rtmp"],
            )

            persisted = IngestCredentialStore(state_dir)
            self.assertIsNone(
                persisted.verify(
                    username="session-1",
                    secret=secret,
                    protocol="rtmp",
                    scope="INGEST",
                )
            )

    def test_corrupt_state_is_not_treated_as_an_empty_store(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            Path(state_dir, "ingest_credentials.json").write_text(
                "[]", encoding="utf-8"
            )
            with self.assertRaises(IngestCredentialError):
                IngestCredentialStore(state_dir)

    def test_initialized_credential_file_deletion_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            store = IngestCredentialStore(state_dir)
            store.issue(session_id="one", user_id="one")
            Path(state_dir, "ingest_credentials.json").unlink()
            with self.assertRaises(IngestCredentialError):
                store.issue(session_id="two", user_id="two")


class OtherAuthorityStoreSafetyTest(unittest.TestCase):
    def test_entitlement_deletion_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            store = EntitlementStore(state_dir)
            store.set("user-1", max_concurrent_sessions=2)
            Path(state_dir, "entitlements.json").unlink()
            with self.assertRaises(EntitlementStateError):
                store.get("user-1")

    def test_destination_secret_deletion_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            store = DestinationSecretStore(state_dir, master_key=Fernet.generate_key())
            store.put(user_id="user-1", secret_ref="secret-1", value="value")
            Path(state_dir, "destination_secrets.json").unlink()
            with self.assertRaises(DestinationSecretError):
                store.resolve(user_id="user-1", secret_ref="secret-1")


if __name__ == "__main__":
    unittest.main()
