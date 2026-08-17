from __future__ import annotations

import os
import sys
import unittest
import uuid


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from provider.conoha import SessionMetadata, is_safe_session_id, request_id_for  # noqa: E402
from provider.fake_provider import FakeProvider  # noqa: E402


class SessionMetadataTest(unittest.TestCase):
    def test_tags_include_required_metadata(self) -> None:
        meta = SessionMetadata(
            session_id=str(uuid.uuid4()),
            user_id="deadbeef",
            environment="dev",
            delete_after=10**9,
        )
        tags = meta.as_tags()
        self.assertEqual(tags["irlight-managed"], "true")
        self.assertEqual(tags["irlight-session-id"], meta.session_id)
        self.assertEqual(tags["irlight-user-id"], "deadbeef")
        self.assertEqual(tags["irlight-environment"], "dev")
        self.assertIn("irlight-created-at", tags)
        self.assertEqual(tags["irlight-delete-after"], "2001-09-09T01:46:40Z")

    def test_rejects_email_like_user_id(self) -> None:
        with self.assertRaises(ValueError):
            SessionMetadata(
                session_id=str(uuid.uuid4()),
                user_id="user@example.com",
                environment="dev",
            )

    def test_rejects_invalid_environment(self) -> None:
        with self.assertRaises(ValueError):
            SessionMetadata(
                session_id=str(uuid.uuid4()),
                user_id="deadbeef",
                environment="staging",
            )

    def test_is_safe_session_id(self) -> None:
        self.assertTrue(is_safe_session_id(str(uuid.uuid4())))
        self.assertFalse(is_safe_session_id("not-a-uuid"))
        self.assertFalse(is_safe_session_id(""))

    def test_request_id_is_deterministic_and_scoped(self) -> None:
        session_id = str(uuid.uuid4())
        self.assertEqual(
            request_id_for("volume", session_id), request_id_for("volume", session_id)
        )
        self.assertNotEqual(
            request_id_for("volume", session_id), request_id_for("server", session_id)
        )
        self.assertEqual(request_id_for("volume", session_id)[:8], "irlight-")


class FakeProviderLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = FakeProvider()
        self.session_id = str(uuid.uuid4())
        self.metadata = SessionMetadata(
            session_id=self.session_id,
            user_id="deadbeef",
            environment="dev",
            delete_after=10**9 + 3600,
        )

    def test_create_list_delete_lifecycle(self) -> None:
        volume = self.provider.create_volume(
            "boot", 20, self.metadata.as_tags()
        )
        self.assertTrue(volume.is_managed)
        server = self.provider.create_server(
            "node",
            image_ref="ubuntu-24.04",
            flavor_ref="g2",
            volume_id=volume.volume_id,
            metadata=self.metadata.as_tags(),
        )
        self.assertTrue(server.is_managed)

        managed = self.provider.list_managed_resources()
        self.assertEqual(len(managed), 2)
        self.assertEqual(
            {r.kind for r in managed}, {"volume", "server"}
        )
        self.assertTrue(all(r.session_id == self.session_id for r in managed))
        self.assertTrue(all(r.user_id == "deadbeef" for r in managed))

        self.provider.delete_server(server.server_id)
        # boot volume must still exist after server delete (delete_on_termination=false)
        self.assertEqual(len(self.provider.list_managed_resources()), 1)
        self.provider.delete_volume(volume.volume_id)
        self.assertEqual(self.provider.list_managed_resources(), [])

    def test_volume_delete_rejects_attached_volume(self) -> None:
        volume = self.provider.create_volume(
            "boot", 20, self.metadata.as_tags()
        )
        self.provider.create_server(
            "node",
            image_ref="ubuntu-24.04",
            flavor_ref="g2",
            volume_id=volume.volume_id,
            metadata=self.metadata.as_tags(),
        )
        with self.assertRaises(RuntimeError):
            self.provider.delete_volume(volume.volume_id)

    def test_unmanaged_resources_are_hidden(self) -> None:
        self.provider.create_volume("plain", 10, {})
        self.provider.create_volume("managed", 20, self.metadata.as_tags())
        self.assertEqual(len(self.provider.list_managed_resources()), 1)

    def test_delete_after_tag_round_trip(self) -> None:
        volume = self.provider.create_volume(
            "boot", 20, self.metadata.as_tags()
        )
        managed = self.provider.list_managed_resources()
        volume_entry = next(r for r in managed if r.kind == "volume")
        self.assertIsNotNone(volume_entry.delete_after)
        self.assertEqual(volume_entry.session_id, self.session_id)


if __name__ == "__main__":
    unittest.main()
