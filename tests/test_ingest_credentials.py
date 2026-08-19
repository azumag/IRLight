from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from ingest_api import MediaMTXAuthRequest, authorize_mediamtx_publish  # noqa: E402
from ingest_auth_guard import IngestAuthGuard  # noqa: E402
from ingest_store import IngestCredentialStore  # noqa: E402


class _SessionStore:
    def __init__(self, session: dict[str, object] | None) -> None:
        self.session = session

    def get(self, session_id: str):
        if self.session is not None and self.session.get("session_id") == session_id:
            return dict(self.session)
        return None


class IngestCredentialStoreTest(unittest.TestCase):
    def test_raw_secret_is_never_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = IngestCredentialStore(tmp)
            record, secret = store.issue(
                session_id="11111111-1111-4111-8111-111111111111",
                user_id="deadbeef",
                protocols=["rtmp", "srt"],
                now=100.0,
            )
            raw = Path(tmp, "ingest_credentials.json").read_text(encoding="utf-8")
            self.assertNotIn(secret, raw)
            persisted = json.loads(raw)
            stored = persisted["credentials"][record["id"]]
            self.assertEqual(len(stored["secret_sha256"]), 64)
            self.assertNotIn("secret_sha256", record)

    def test_rotation_revokes_old_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = IngestCredentialStore(tmp)
            session_id = "11111111-1111-4111-8111-111111111111"
            _first, first_secret = store.issue(
                session_id=session_id,
                user_id="deadbeef",
                protocols=["rtmp"],
                now=100.0,
            )
            second, second_secret = store.issue(
                session_id=session_id,
                user_id="deadbeef",
                protocols=["rtmp"],
                now=101.0,
            )
            self.assertIsNone(
                store.verify(
                    username=session_id,
                    secret=first_secret,
                    protocol="rtmp",
                    now=102.0,
                )
            )
            verified = store.verify(
                username=session_id,
                secret=second_secret,
                protocol="rtmp",
                now=102.0,
            )
            self.assertEqual(verified["id"], second["id"])

    def test_protocol_expiry_and_revoke_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = IngestCredentialStore(tmp)
            session_id = "11111111-1111-4111-8111-111111111111"
            record, secret = store.issue(
                session_id=session_id,
                user_id="deadbeef",
                protocols=["srt"],
                ttl_seconds=10,
                now=100.0,
            )
            self.assertIsNone(
                store.verify(username=session_id, secret=secret, protocol="rtmp", now=105.0)
            )
            self.assertIsNotNone(
                store.verify(username=session_id, secret=secret, protocol="srt", now=105.0)
            )
            self.assertIsNone(
                store.verify(username=session_id, secret=secret, protocol="srt", now=110.0)
            )

            rotated, rotated_secret = store.issue(
                session_id=session_id,
                user_id="deadbeef",
                protocols=["srt"],
                now=200.0,
            )
            store.revoke(rotated["id"], user_id="deadbeef")
            self.assertIsNone(
                store.verify(
                    username=session_id,
                    secret=rotated_secret,
                    protocol="srt",
                    now=201.0,
                )
            )

    def test_revoke_session_invalidates_all_active_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = IngestCredentialStore(tmp)
            session_id = "11111111-1111-4111-8111-111111111111"
            _record, secret = store.issue(
                session_id=session_id,
                user_id="deadbeef",
                protocols=["rtmp", "srt"],
                now=100.0,
            )
            self.assertEqual(store.revoke_session(session_id, now=101.0), 1)
            self.assertIsNone(
                store.verify(username=session_id, secret=secret, protocol="rtmp", now=102.0)
            )


class MediaMTXAuthTest(unittest.TestCase):
    def test_valid_publish_is_authorized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            credential_store = IngestCredentialStore(tmp)
            auth_guard = IngestAuthGuard(tmp)
            session_id = "11111111-1111-4111-8111-111111111111"
            record, secret = credential_store.issue(
                session_id=session_id,
                user_id="deadbeef",
                protocols=["rtmp", "srt"],
            )
            session_store = _SessionStore(
                {"session_id": session_id, "user_id": "deadbeef", "status": "READY_WAIT_INGEST"}
            )
            request = MediaMTXAuthRequest(
                user=session_id,
                password=secret,
                action="publish",
                path="live/input",
                protocol="rtmp",
                id="publisher-1",
            )
            with patch("ingest_api.default_store", return_value=session_store), patch(
                "ingest_api.default_ingest_store", return_value=credential_store
            ), patch("ingest_api.default_ingest_auth_guard", return_value=auth_guard):
                result = authorize_mediamtx_publish(request)
            self.assertTrue(result["authorized"])
            self.assertEqual(result["credential_id"], record["id"])

    def test_wrong_secret_and_finished_session_are_rejected_generically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            credential_store = IngestCredentialStore(tmp)
            auth_guard = IngestAuthGuard(tmp)
            session_id = "11111111-1111-4111-8111-111111111111"
            _record, secret = credential_store.issue(
                session_id=session_id,
                user_id="deadbeef",
                protocols=["rtmp"],
            )
            request = MediaMTXAuthRequest(
                user=session_id,
                password="wrong-secret",
                action="publish",
                path="live/input",
                protocol="rtmp",
            )
            ready_store = _SessionStore(
                {"session_id": session_id, "user_id": "deadbeef", "status": "READY_WAIT_INGEST"}
            )
            with patch("ingest_api.default_store", return_value=ready_store), patch(
                "ingest_api.default_ingest_store", return_value=credential_store
            ), patch("ingest_api.default_ingest_auth_guard", return_value=auth_guard):
                with self.assertRaises(HTTPException) as wrong:
                    authorize_mediamtx_publish(request)
            self.assertEqual(wrong.exception.status_code, 401)
            self.assertEqual(wrong.exception.detail, "invalid ingest credential")

            request.password = secret
            finished_store = _SessionStore(
                {"session_id": session_id, "user_id": "deadbeef", "status": "FINISHED"}
            )
            with patch("ingest_api.default_store", return_value=finished_store), patch(
                "ingest_api.default_ingest_store", return_value=credential_store
            ), patch("ingest_api.default_ingest_auth_guard", return_value=auth_guard):
                with self.assertRaises(HTTPException) as finished:
                    authorize_mediamtx_publish(request)
            self.assertEqual(finished.exception.status_code, 401)
            self.assertEqual(finished.exception.detail, "invalid ingest credential")

    def test_non_ingest_actions_paths_and_protocols_are_rejected(self) -> None:
        for request in (
            MediaMTXAuthRequest(action="read", path="live/input", protocol="rtmp"),
            MediaMTXAuthRequest(action="publish", path="other", protocol="rtmp"),
            MediaMTXAuthRequest(action="publish", path="live/input", protocol="rtsp"),
        ):
            with self.assertRaises(HTTPException) as failure:
                authorize_mediamtx_publish(request)
            self.assertEqual(failure.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
