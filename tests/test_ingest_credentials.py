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

from ingest_api import (  # noqa: E402
    INGEST_CLOCK_UNAVAILABLE,
    INGEST_DEADLINE_UNAVAILABLE,
    IssueIngestCredentialRequest,
    MediaMTXAuthRequest,
    authorize_mediamtx_publish,
    issue_ingest_credential,
    revoke_ingest_credential,
)
from ingest_auth_guard import IngestAuthGuard  # noqa: E402
from ingest_store import IngestCredentialStore  # noqa: E402


class _SessionStore:
    def __init__(self, session: dict[str, object] | None) -> None:
        self.session = session
        self.events: list[dict[str, object]] = []

    def get(self, session_id: str):
        if self.session is not None and self.session.get("session_id") == session_id:
            return dict(self.session)
        return None

    def append_event(self, session_id: str, **kwargs):
        if self.get(session_id) is None:
            raise KeyError(session_id)
        event = dict(kwargs)
        self.events.append(event)
        return event


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
                store.verify(username=session_id, secret=first_secret, protocol="rtmp", now=102.0)
            )
            verified = store.verify(
                username=session_id, secret=second_secret, protocol="rtmp", now=102.0
            )
            self.assertEqual(verified["id"], second["id"])

    def test_protocol_expiry_and_revoke_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = IngestCredentialStore(tmp)
            session_id = "11111111-1111-4111-8111-111111111111"
            _record, secret = store.issue(
                session_id=session_id,
                user_id="deadbeef",
                protocols=["srt"],
                ttl_seconds=10,
                now=100.0,
            )
            self.assertIsNone(store.verify(username=session_id, secret=secret, protocol="rtmp", now=105.0))
            self.assertIsNotNone(store.verify(username=session_id, secret=secret, protocol="srt", now=105.0))
            self.assertIsNone(store.verify(username=session_id, secret=secret, protocol="srt", now=110.0))
            rotated, rotated_secret = store.issue(
                session_id=session_id, user_id="deadbeef", protocols=["srt"], now=200.0
            )
            store.revoke(rotated["id"], user_id="deadbeef")
            self.assertIsNone(store.verify(username=session_id, secret=rotated_secret, protocol="srt", now=201.0))

    def test_revoke_session_invalidates_all_active_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = IngestCredentialStore(tmp)
            session_id = "11111111-1111-4111-8111-111111111111"
            _record, secret = store.issue(
                session_id=session_id, user_id="deadbeef", protocols=["rtmp", "srt"], now=100.0
            )
            self.assertEqual(store.revoke_session(session_id, now=101.0), 1)
            self.assertIsNone(store.verify(username=session_id, secret=secret, protocol="rtmp", now=102.0))




class IngestCredentialEndpointPreflightTest(unittest.TestCase):
    def test_invalid_ingest_port_does_not_rotate_existing_credential(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            credential_store = IngestCredentialStore(tmp)
            session_id = "11111111-1111-4111-8111-111111111111"
            old_record, old_secret = credential_store.issue(
                session_id=session_id,
                user_id="user-1",
                protocols=["rtmp"],
                now=100.0,
            )
            session_store = _SessionStore(
                {
                    "session_id": session_id,
                    "user_id": "user-1",
                    "status": "READY_WAIT_INGEST",
                }
            )

            with patch.dict(
                "os.environ",
                {"IRLIGHT_INGEST_RTMP_PORT": "broken"},
                clear=True,
            ), patch(
                "ingest_api.default_store",
                return_value=session_store,
            ), patch(
                "ingest_api.default_ingest_store",
                return_value=credential_store,
            ), patch.object(
                credential_store,
                "issue",
                wraps=credential_store.issue,
            ) as issue:
                with self.assertRaises(HTTPException) as failure:
                    issue_ingest_credential(
                        session_id,
                        IssueIngestCredentialRequest(protocols=["rtmp"]),
                        {"id": "user-1"},
                    )

            self.assertEqual(failure.exception.status_code, 503)
            self.assertEqual(
                failure.exception.detail,
                "ingest endpoint configuration unavailable",
            )
            issue.assert_not_called()
            verified = credential_store.verify(
                username=session_id,
                secret=old_secret,
                protocol="rtmp",
                now=101.0,
            )
            self.assertIsNotNone(verified)
            self.assertEqual(verified["id"], old_record["id"])

    def test_invalid_relay_port_does_not_rotate_existing_credential(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            credential_store = IngestCredentialStore(tmp)
            session_id = "11111111-1111-4111-8111-111111111111"
            old_record, old_secret = credential_store.issue(
                session_id=session_id,
                user_id="user-1",
                scope="RELAY_CLIENT",
                protocols=["rtmp"],
                now=100.0,
            )
            session_store = _SessionStore(
                {
                    "session_id": session_id,
                    "user_id": "user-1",
                    "status": "READY_WAIT_INGEST",
                    "egress_mode": "RELAY_ONLY",
                }
            )

            with patch.dict(
                "os.environ",
                {"IRLIGHT_RELAY_RTMP_PORT": "65536"},
                clear=True,
            ), patch(
                "ingest_api.default_store",
                return_value=session_store,
            ), patch(
                "ingest_api.default_ingest_store",
                return_value=credential_store,
            ), patch.object(
                credential_store,
                "issue",
                wraps=credential_store.issue,
            ) as issue:
                with self.assertRaises(HTTPException) as failure:
                    issue_ingest_credential(
                        session_id,
                        IssueIngestCredentialRequest(
                            scope="RELAY_CLIENT",
                            protocols=["rtmp"],
                        ),
                        {"id": "user-1"},
                    )

            self.assertEqual(failure.exception.status_code, 503)
            self.assertEqual(
                failure.exception.detail,
                "ingest endpoint configuration unavailable",
            )
            issue.assert_not_called()
            verified = credential_store.verify(
                username=session_id,
                secret=old_secret,
                protocol="rtmp",
                scope="RELAY_CLIENT",
                now=101.0,
            )
            self.assertIsNotNone(verified)
            self.assertEqual(verified["id"], old_record["id"])

class IngestCredentialDeadlineClockBoundaryTest(unittest.TestCase):
    """Credential issuance must enforce the Session absolute deadline against a
    validated effective clock and never treat a damaged deadline as "no limit"."""

    SESSION_ID = "11111111-1111-4111-8111-111111111111"
    USER_ID = "user-1"

    def _session_store(self, **extra: object) -> "_SessionStore":
        session: dict[str, object] = {
            "session_id": self.SESSION_ID,
            "user_id": self.USER_ID,
            "status": "READY_WAIT_INGEST",
        }
        session.update(extra)
        return _SessionStore(session)

    def _issue(self, credential_store, session_store, clock, **request_kwargs):
        request = IssueIngestCredentialRequest(**request_kwargs)
        with patch("ingest_api.default_store", return_value=session_store), patch(
            "ingest_api.default_ingest_store", return_value=credential_store
        ), patch("ingest_api.time.time", return_value=clock):
            return issue_ingest_credential(
                self.SESSION_ID, request, {"id": self.USER_ID}
            )

    def test_invalid_clock_fails_closed_without_rotating_credential(self) -> None:
        invalid_clocks = (
            float("nan"),
            float("inf"),
            -float("inf"),
            -5.0,
            True,
            "100",
            10**10000,
        )
        for index, clock in enumerate(invalid_clocks):
            with self.subTest(case=index, value_type=type(clock).__name__):
                with tempfile.TemporaryDirectory() as tmp:
                    credential_store = IngestCredentialStore(tmp)
                    old_record, old_secret = credential_store.issue(
                        session_id=self.SESSION_ID,
                        user_id=self.USER_ID,
                        protocols=["rtmp"],
                        now=100.0,
                    )
                    session_store = self._session_store(absolute_deadline_at=1000.0)

                    with patch.object(
                        credential_store, "issue", wraps=credential_store.issue
                    ) as issue:
                        with self.assertRaises(HTTPException) as failure:
                            self._issue(
                                credential_store,
                                session_store,
                                clock,
                                protocols=["rtmp"],
                            )

                    self.assertEqual(failure.exception.status_code, 503)
                    self.assertEqual(
                        failure.exception.detail, INGEST_CLOCK_UNAVAILABLE
                    )
                    issue.assert_not_called()
                    verified = credential_store.verify(
                        username=self.SESSION_ID,
                        secret=old_secret,
                        protocol="rtmp",
                        now=101.0,
                    )
                    self.assertIsNotNone(verified)
                    self.assertEqual(verified["id"], old_record["id"])

    def test_invalid_clock_fails_closed_without_a_deadline(self) -> None:
        # A Session without an absolute deadline still must not surface an
        # unhandled serializer/clock exception to the caller.
        for clock in (float("nan"), -float("inf")):
            with tempfile.TemporaryDirectory() as tmp:
                credential_store = IngestCredentialStore(tmp)
                session_store = self._session_store()
                with self.assertRaises(HTTPException) as failure:
                    self._issue(
                        credential_store, session_store, clock, protocols=["rtmp"]
                    )
                self.assertEqual(failure.exception.status_code, 503)
                self.assertEqual(failure.exception.detail, INGEST_CLOCK_UNAVAILABLE)
                self.assertIsNone(
                    credential_store.active_for_session(self.SESSION_ID, now=1.0)
                )

    def test_damaged_persisted_deadline_fails_closed(self) -> None:
        invalid_deadlines = (
            "soon",
            float("nan"),
            float("inf"),
            -float("inf"),
            -1.0,
            True,
            10**10000,
        )
        for index, deadline in enumerate(invalid_deadlines):
            with self.subTest(case=index, value_type=type(deadline).__name__):
                with tempfile.TemporaryDirectory() as tmp:
                    credential_store = IngestCredentialStore(tmp)
                    old_record, old_secret = credential_store.issue(
                        session_id=self.SESSION_ID,
                        user_id=self.USER_ID,
                        protocols=["rtmp"],
                        now=100.0,
                    )
                    session_store = self._session_store(absolute_deadline_at=deadline)

                    with patch.object(
                        credential_store, "issue", wraps=credential_store.issue
                    ) as issue:
                        with self.assertRaises(HTTPException) as failure:
                            self._issue(
                                credential_store,
                                session_store,
                                100.0,
                                protocols=["rtmp"],
                            )

                    self.assertEqual(failure.exception.status_code, 503)
                    self.assertEqual(
                        failure.exception.detail, INGEST_DEADLINE_UNAVAILABLE
                    )
                    issue.assert_not_called()
                    verified = credential_store.verify(
                        username=self.SESSION_ID,
                        secret=old_secret,
                        protocol="rtmp",
                        now=101.0,
                    )
                    self.assertIsNotNone(verified)
                    self.assertEqual(verified["id"], old_record["id"])

    def test_expired_deadline_is_rejected_with_a_valid_clock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            credential_store = IngestCredentialStore(tmp)
            session_store = self._session_store(absolute_deadline_at=99.0)
            with self.assertRaises(HTTPException) as failure:
                self._issue(credential_store, session_store, 100.0, protocols=["rtmp"])
            self.assertEqual(failure.exception.status_code, 409)
            self.assertEqual(
                failure.exception.detail, "session deadline has expired"
            )
            # The exact deadline instant is already expired.
            session_store = self._session_store(absolute_deadline_at=100.0)
            with self.assertRaises(HTTPException) as equal:
                self._issue(credential_store, session_store, 100.0, protocols=["rtmp"])
            self.assertEqual(equal.exception.status_code, 409)

    def test_valid_clock_caps_ttl_at_the_session_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            credential_store = IngestCredentialStore(tmp)
            session_store = self._session_store(absolute_deadline_at=160.0)
            record = self._issue(
                credential_store, session_store, 100.0, protocols=["rtmp"]
            )
            self.assertEqual(record["created_at"], 100.0)
            self.assertEqual(record["expires_at"], 160.0)

    def test_epoch_zero_clock_remains_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            credential_store = IngestCredentialStore(tmp)
            session_store = self._session_store()
            record = self._issue(
                credential_store, session_store, 0.0, protocols=["rtmp"]
            )
            self.assertEqual(record["created_at"], 0.0)


class MediaMTXAuthTest(unittest.TestCase):
    def test_valid_publish_is_authorized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            credential_store = IngestCredentialStore(tmp)
            auth_guard = IngestAuthGuard(tmp)
            session_id = "11111111-1111-4111-8111-111111111111"
            record, secret = credential_store.issue(
                session_id=session_id, user_id="deadbeef", protocols=["rtmp", "srt"]
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

    def test_wrong_secret_records_secret_free_session_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            credential_store = IngestCredentialStore(tmp)
            auth_guard = IngestAuthGuard(tmp)
            session_id = "11111111-1111-4111-8111-111111111111"
            _record, secret = credential_store.issue(
                session_id=session_id, user_id="deadbeef", protocols=["rtmp"]
            )
            request = MediaMTXAuthRequest(
                user=session_id,
                password="wrong-secret",
                ip="203.0.113.10",
                action="publish",
                path="live/input",
                protocol="rtmp",
                id="publisher-2",
            )
            ready_store = _SessionStore(
                {"session_id": session_id, "user_id": "deadbeef", "status": "READY_WAIT_INGEST", "node_id": "node-1"}
            )
            with patch("ingest_api.default_store", return_value=ready_store), patch(
                "ingest_api.default_ingest_store", return_value=credential_store
            ), patch("ingest_api.default_ingest_auth_guard", return_value=auth_guard):
                with self.assertRaises(HTTPException) as wrong:
                    authorize_mediamtx_publish(request)
            self.assertEqual(wrong.exception.status_code, 401)
            self.assertEqual(len(ready_store.events), 1)
            event = ready_store.events[0]
            self.assertEqual(event["event_type"], "ingest.auth_failed")
            self.assertEqual(event["reason_code"], "INVALID_CREDENTIAL")
            self.assertEqual(event["origin"], "ingest-auth")
            payload = event["payload"]
            self.assertEqual(payload["source_ip"], "203.0.113.10")
            self.assertNotIn("password", payload)
            self.assertNotIn("credential_secret", payload)
            self.assertNotIn("wrong-secret", json.dumps(event))
            self.assertNotIn(secret, json.dumps(event))

    def test_finished_session_is_rejected_generically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            credential_store = IngestCredentialStore(tmp)
            auth_guard = IngestAuthGuard(tmp)
            session_id = "11111111-1111-4111-8111-111111111111"
            _record, secret = credential_store.issue(
                session_id=session_id, user_id="deadbeef", protocols=["rtmp"]
            )
            request = MediaMTXAuthRequest(
                user=session_id,
                password=secret,
                action="publish",
                path="live/input",
                protocol="rtmp",
            )
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


class RelayCredentialApiTest(unittest.TestCase):
    def test_direct_push_session_cannot_issue_relay_credential(self) -> None:
        session_id = "11111111-1111-4111-8111-111111111111"
        session_store = _SessionStore(
            {
                "session_id": session_id,
                "user_id": "user-1",
                "status": "READY_WAIT_INGEST",
                "egress_mode": "DIRECT_PUSH",
            }
        )
        with patch("ingest_api.default_store", return_value=session_store):
            with self.assertRaises(HTTPException) as failure:
                issue_ingest_credential(
                    session_id,
                    IssueIngestCredentialRequest(
                        scope="RELAY_CLIENT", protocols=["rtmp"]
                    ),
                    {"id": "user-1"},
                )
        self.assertEqual(failure.exception.status_code, 409)

    def test_relay_credential_can_be_revoked_by_its_id(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            session_id = "11111111-1111-4111-8111-111111111111"
            credential_store = IngestCredentialStore(state_dir)
            record, secret = credential_store.issue(
                session_id=session_id,
                user_id="user-1",
                scope="RELAY_CLIENT",
                protocols=["rtmp"],
            )
            session_store = _SessionStore(
                {
                    "session_id": session_id,
                    "user_id": "user-1",
                    "status": "READY_WAIT_INGEST",
                    "egress_mode": "RELAY_ONLY",
                }
            )
            with patch("ingest_api.default_store", return_value=session_store), patch(
                "ingest_api.default_ingest_store", return_value=credential_store
            ):
                revoked = revoke_ingest_credential(
                    session_id,
                    str(record["id"]),
                    {"id": "user-1"},
                )
            self.assertIsNotNone(revoked["revoked_at"])
            self.assertIsNone(
                credential_store.verify(
                    username=session_id,
                    secret=secret,
                    protocol="rtmp",
                    scope="RELAY_CLIENT",
                )
            )


if __name__ == "__main__":
    unittest.main()
