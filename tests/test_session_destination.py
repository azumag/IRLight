from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from fastapi import HTTPException


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

import node_internal  # noqa: E402
import session_api  # noqa: E402
from destination_secret_store import (  # noqa: E402
    DestinationSecretError,
    DestinationSecretNotFound,
    DestinationSecretStore,
)


class _SecretStore:
    def __init__(self, *, configured: bool = True, value: str = "stream-key") -> None:
        self.is_configured = configured
        self.value = value

    def configured(self, *, user_id: str, secret_ref: str) -> bool:
        return self.is_configured

    def resolve(self, *, user_id: str, secret_ref: str) -> str:
        if not self.is_configured:
            raise DestinationSecretNotFound(secret_ref)
        return self.value


class _EmptySessionStore:
    def get(self, session_id: str) -> None:
        return None


DESTINATION = {
    "id": "dest-1",
    "user_id": "user-1",
    "type": "rtmps",
    "display_name": "Primary",
    "server_url": "rtmps://example.invalid/live/{stream_key}",
    "secret_ref": "dest/primary",
    "enabled": True,
    "verification_status": "VERIFIED",
}


class PrepareDestinationTest(unittest.TestCase):
    def test_verified_enabled_destination_with_secret_is_accepted(self) -> None:
        with patch("session_api.store_get_destination", return_value=dict(DESTINATION)), patch(
            "session_api.default_destination_secret_store", return_value=_SecretStore()
        ):
            result = session_api._validated_destination("dest-1", "user-1")
        self.assertEqual(result["id"], "dest-1")

    def test_disabled_unverified_unsupported_or_missing_secret_is_rejected(self) -> None:
        cases = [
            ({**DESTINATION, "enabled": False}, _SecretStore(), "disabled"),
            ({**DESTINATION, "verification_status": "FAILED"}, _SecretStore(), "verified"),
            ({**DESTINATION, "type": "srt"}, _SecretStore(), "not supported"),
            (dict(DESTINATION), _SecretStore(configured=False), "secret is not configured"),
            (
                {**DESTINATION, "type": "rtmp"},
                _SecretStore(),
                "configuration is invalid",
            ),
        ]
        for destination, secret_store, detail in cases:
            with self.subTest(detail=detail), patch(
                "session_api.store_get_destination", return_value=destination
            ), patch(
                "session_api.default_destination_secret_store", return_value=secret_store
            ):
                with self.assertRaises(HTTPException) as failure:
                    session_api._validated_destination("dest-1", "user-1")
                self.assertEqual(failure.exception.status_code, 409)
                self.assertIn(detail, str(failure.exception.detail))

    def test_wrong_master_key_fails_before_provider_is_selected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = DestinationSecretStore(tmp, master_key=Fernet.generate_key())
            first.put(
                user_id="user-1",
                secret_ref="dest/primary",
                value="stream-key",
            )
            wrong_key_store = DestinationSecretStore(
                tmp,
                master_key=Fernet.generate_key(),
            )
            with patch("session_api.default_store", return_value=_EmptySessionStore()), patch(
                "session_api.store_get_destination", return_value=dict(DESTINATION)
            ), patch(
                "session_api.default_destination_secret_store",
                return_value=wrong_key_store,
            ), patch("session_api.default_provider") as provider_factory:
                with self.assertRaises(HTTPException) as failure:
                    session_api.prepare_session(
                        "session-wrong-key",
                        session_api.PrepareRequest(
                            environment="prod",
                            destination_id="dest-1",
                        ),
                        {"id": "user-1"},
                        idempotency_key="wrong-key-preflight",
                        _csrf=None,
                    )
            self.assertEqual(failure.exception.status_code, 503)
            self.assertIn("secret is unavailable", str(failure.exception.detail))
            provider_factory.assert_not_called()

    def test_explicit_secret_error_is_rejected_by_validation(self) -> None:
        class _BrokenSecretStore(_SecretStore):
            def resolve(self, *, user_id: str, secret_ref: str) -> str:
                raise DestinationSecretError("broken ciphertext")

        with patch("session_api.store_get_destination", return_value=dict(DESTINATION)), patch(
            "session_api.default_destination_secret_store",
            return_value=_BrokenSecretStore(),
        ):
            with self.assertRaises(HTTPException) as failure:
                session_api._validated_destination("dest-1", "user-1")
        self.assertEqual(failure.exception.status_code, 503)
        self.assertEqual(failure.exception.detail, "destination secret is unavailable")

    def test_conoha_requires_destination_by_default(self) -> None:
        with patch("session_api.provider_mode", return_value="conoha"):
            with self.assertRaises(HTTPException) as failure:
                session_api._validated_destination(None, "user-1")
        self.assertEqual(failure.exception.status_code, 409)
        self.assertEqual(failure.exception.detail, "destination_id is required")

    def test_fake_provider_keeps_destination_optional_for_poc(self) -> None:
        with patch("session_api.provider_mode", return_value="fake"):
            self.assertIsNone(session_api._validated_destination(None, "user-1"))


class BootstrapEgressResolutionTest(unittest.TestCase):
    def test_assigned_session_resolves_secret_only_for_bootstrap(self) -> None:
        session = {
            "session_id": "session-1",
            "user_id": "user-1",
            "destination_id": "dest-1",
        }
        secret = "secret/with?characters"
        with patch("node_internal.store_get_destination", return_value=dict(DESTINATION)), patch(
            "node_internal.default_destination_secret_store",
            return_value=_SecretStore(value=secret),
        ):
            result = node_internal._resolve_egress_url(session)
        self.assertEqual(
            result,
            "rtmps://example.invalid/live/secret%2Fwith%3Fcharacters",
        )
        self.assertNotIn(secret, str(session))

    def test_unassigned_legacy_node_uses_static_egress(self) -> None:
        with patch.dict("os.environ", {"NODE_EGRESS_URL": "rtmp://internal/output/relay"}):
            self.assertEqual(
                node_internal._resolve_egress_url(None),
                "rtmp://internal/output/relay",
            )


if __name__ == "__main__":
    unittest.main()
