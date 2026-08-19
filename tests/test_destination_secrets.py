from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from destination_secret_store import (  # noqa: E402
    DestinationSecretError,
    DestinationSecretNotFound,
    DestinationSecretStore,
)
from egress_destination import EgressDestinationError, build_egress_url  # noqa: E402


class DestinationSecretStoreTest(unittest.TestCase):
    def test_secret_is_encrypted_at_rest_and_resolves(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            key = Fernet.generate_key()
            store = DestinationSecretStore(tmp, master_key=key)
            secret = "live_stream_key-do-not-log"
            result = store.put(
                user_id="user-a",
                secret_ref="dest/main",
                value=secret,
                now=100.0,
            )
            self.assertTrue(result["configured"])
            raw = Path(tmp, "destination_secrets.json").read_text(encoding="utf-8")
            self.assertNotIn(secret, raw)
            self.assertEqual(
                store.resolve(user_id="user-a", secret_ref="dest/main"), secret
            )

    def test_same_ref_is_isolated_by_user(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = DestinationSecretStore(tmp, master_key=Fernet.generate_key())
            store.put(user_id="user-a", secret_ref="primary", value="alpha")
            store.put(user_id="user-b", secret_ref="primary", value="bravo")
            self.assertEqual(store.resolve(user_id="user-a", secret_ref="primary"), "alpha")
            self.assertEqual(store.resolve(user_id="user-b", secret_ref="primary"), "bravo")

    def test_wrong_master_key_cannot_decrypt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = DestinationSecretStore(tmp, master_key=Fernet.generate_key())
            first.put(user_id="user-a", secret_ref="primary", value="secret-value")
            second = DestinationSecretStore(tmp, master_key=Fernet.generate_key())
            with self.assertRaises(DestinationSecretError):
                second.resolve(user_id="user-a", secret_ref="primary")

    def test_corrupt_json_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "destination_secrets.json").write_text(
                "{not-valid-json",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(DestinationSecretError, "invalid JSON"):
                DestinationSecretStore(tmp, master_key=Fernet.generate_key())

    def test_unreadable_state_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch(
                "destination_secret_store.Path.open",
                side_effect=PermissionError("permission denied"),
            ):
                with self.assertRaisesRegex(DestinationSecretError, "cannot be read"):
                    DestinationSecretStore(tmp, master_key=Fernet.generate_key())

    def test_invalid_state_record_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "destination_secrets.json").write_text(
                '{"secrets":{"not-a-valid-key":{"user_id":"user-a","secret_ref":"primary","ciphertext":"ciphertext"}}}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(DestinationSecretError, "mismatched record key"):
                DestinationSecretStore(tmp, master_key=Fernet.generate_key())

    def test_delete_and_missing_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = DestinationSecretStore(tmp, master_key=Fernet.generate_key())
            store.put(user_id="user-a", secret_ref="primary", value="secret-value")
            self.assertTrue(store.delete(user_id="user-a", secret_ref="primary"))
            self.assertFalse(store.delete(user_id="user-a", secret_ref="primary"))
            with self.assertRaises(DestinationSecretNotFound):
                store.resolve(user_id="user-a", secret_ref="primary")


class EgressDestinationTest(unittest.TestCase):
    def test_stream_key_placeholder_is_encoded(self) -> None:
        destination = {
            "type": "rtmps",
            "server_url": "rtmps://example.invalid/live/{stream_key}",
        }
        self.assertEqual(
            build_egress_url(destination, "abc/def?x=1"),
            "rtmps://example.invalid/live/abc%2Fdef%3Fx%3D1",
        )

    def test_stream_key_is_appended_before_query(self) -> None:
        destination = {
            "type": "rtmp",
            "server_url": "rtmp://example.invalid/app?mode=publish",
        }
        self.assertEqual(
            build_egress_url(destination, "stream-key"),
            "rtmp://example.invalid/app/stream-key?mode=publish",
        )

    def test_unsupported_srt_egress_is_rejected_for_now(self) -> None:
        with self.assertRaises(EgressDestinationError):
            build_egress_url(
                {"type": "srt", "server_url": "srt://example.invalid:9000"},
                "secret",
            )


if __name__ == "__main__":
    unittest.main()
