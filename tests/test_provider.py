from __future__ import annotations

import os
import sys
import unittest
import uuid
import json
from unittest.mock import patch


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from provider.conoha import (  # noqa: E402
    SessionMetadata,
    is_safe_session_id,
    is_safe_user_id,
    parse_timestamp,
    request_id_for,
)
from provider.fake_provider import FakeProvider  # noqa: E402
from provider.provider_client import ConohaClient, ConohaConfig  # noqa: E402


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

    def test_accepts_authenticated_uuid_user_id(self) -> None:
        user_id = str(uuid.uuid4())
        self.assertTrue(is_safe_user_id(user_id))
        meta = SessionMetadata(
            session_id=str(uuid.uuid4()),
            user_id=user_id,
            environment="dev",
        )
        self.assertEqual(meta.as_tags()["irlight-user-id"], user_id)

    def test_rejects_email_like_user_id(self) -> None:
        self.assertFalse(is_safe_user_id("user@example.com"))
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

    def test_utc_timestamp_parse_does_not_depend_on_host_timezone(self) -> None:
        self.assertEqual(parse_timestamp("2001-09-09T01:46:40Z"), 1_000_000_000)


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class ConohaClientAuthenticationTest(unittest.TestCase):
    @staticmethod
    def _config() -> ConohaConfig:
        return ConohaConfig(
            identity_endpoint="https://identity.example/v2.0",
            compute_endpoint="https://compute.example/v2/tenant",
            volume_endpoint="https://volume.example/v2/tenant",
            username="user",
            password="password",
            tenant_name="tenant",
        )

    def test_identity_request_does_not_recursively_require_a_token(self) -> None:
        config = ConohaConfig(
            identity_endpoint="https://identity.example/v2.0",
            compute_endpoint="https://compute.example/v2",
            volume_endpoint="https://volume.example/v2",
            username="user",
            password="password",
            tenant_name="tenant",
        )
        client = ConohaClient(config)
        requests = []

        def fake_urlopen(request, *, timeout):
            requests.append(request)
            if request.full_url.endswith("/tokens"):
                self.assertIsNone(request.get_header("X-auth-token"))
                return _Response(
                    {
                        "access": {
                            "token": {
                                "id": "token-1",
                                "expires": "2099-01-01T00:00:00Z",
                            }
                        }
                    }
                )
            self.assertEqual(request.get_header("X-auth-token"), "token-1")
            return _Response({"volumes": []})

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            self.assertEqual(client.list_volumes(), [])

        self.assertEqual([request.method for request in requests], ["POST", "GET"])

    def test_server_inventory_uses_detail_and_follows_every_page(self) -> None:
        client = ConohaClient(self._config())
        pages = [
            {
                "servers": [
                    {
                        "id": "server-1",
                        "name": "one",
                        "status": "ACTIVE",
                        "metadata": {"irlight-managed": "true"},
                        "addresses": {"public": [{"version": 4, "addr": "198.51.100.1"}]},
                    }
                ],
                "servers_links": [
                    {
                        "rel": "next",
                        "href": "https://compute.example/v2/tenant/servers/detail?marker=server-1",
                    }
                ],
            },
            {
                "servers": [
                    {
                        "id": "server-2",
                        "name": "two",
                        "status": "SHUTOFF",
                        "metadata": {"irlight-session-id": "session-2"},
                    }
                ],
                "servers_links": [],
            },
        ]
        with patch.object(client, "_request", side_effect=pages) as request:
            servers = client.list_servers()

        self.assertEqual([server.server_id for server in servers], ["server-1", "server-2"])
        self.assertEqual(servers[0].public_ipv4, "198.51.100.1")
        self.assertEqual(servers[1].tags["irlight-session-id"], "session-2")
        self.assertEqual(
            [call.args[1] for call in request.call_args_list],
            [
                "https://compute.example/v2/tenant/servers/detail",
                "https://compute.example/v2/tenant/servers/detail?marker=server-1",
            ],
        )

    def test_inventory_refuses_cross_origin_pagination(self) -> None:
        client = ConohaClient(self._config())
        with patch.object(
            client,
            "_request",
            return_value={
                "servers": [],
                "servers_links": [
                    {"rel": "next", "href": "https://attacker.example/steal"}
                ],
            },
        ):
            with self.assertRaisesRegex(RuntimeError, "cross-origin"):
                client.list_servers()


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
