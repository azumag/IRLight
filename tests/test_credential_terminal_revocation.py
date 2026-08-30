from __future__ import annotations

import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from fastapi import HTTPException


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "provider"))
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from fake_provider import FakeProvider  # noqa: E402
from ingest_api import (  # noqa: E402
    MediaMTXAuthRequest,
    authorize_mediamtx_request,
)
from ingest_auth_guard import IngestAuthGuard  # noqa: E402
from ingest_store import IngestCredentialStore  # noqa: E402
from reaper import Reaper, ReaperConfig  # noqa: E402
from session_store import SessionStore  # noqa: E402
from session_workflow import ProvisioningWorkflow, WorkflowConfig  # noqa: E402


class _SessionStore:
    def __init__(self, session: dict[str, object]):
        self.session = session

    def get(self, session_id: str):
        if self.session.get("session_id") == session_id:
            return dict(self.session)
        return None


class RelayAuthorizationTest(unittest.TestCase):
    def _authorize(
        self,
        credential_store: IngestCredentialStore,
        request,
        state_dir: str,
        *,
        egress_mode: str = "RELAY_ONLY",
    ):
        session_id = "11111111-1111-4111-8111-111111111111"
        session_store = _SessionStore(
            {
                "session_id": session_id,
                "user_id": "user-1",
                "status": "READY_WAIT_INGEST",
                "egress_mode": egress_mode,
            }
        )
        with patch("ingest_api.default_store", return_value=session_store), patch(
            "ingest_api.default_ingest_store", return_value=credential_store
        ), patch(
            "ingest_api.default_ingest_auth_guard",
            return_value=IngestAuthGuard(state_dir),
        ), patch("ingest_api.require_assigned_node") as require_node:
            result = authorize_mediamtx_request(
                request,
                authorization="Bearer node-access-token",
            )
            require_node.assert_called_once_with(
                "Bearer node-access-token",
                session_id=session_id,
            )
            return result

    def test_relay_client_credential_authorizes_output_read(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            store = IngestCredentialStore(state_dir)
            session_id = "11111111-1111-4111-8111-111111111111"
            record, secret = store.issue(
                session_id=session_id,
                user_id="user-1",
                scope="RELAY_CLIENT",
                protocols=["rtmp"],
            )
            result = self._authorize(
                store,
                MediaMTXAuthRequest(
                    user=session_id,
                    password=secret,
                    action="read",
                    path="output/relay",
                    protocol="rtmp",
                ),
                state_dir,
            )
        self.assertTrue(result["authorized"])
        self.assertEqual(result["credential_id"], record["id"])

    def test_ingest_credential_cannot_read_relay(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            store = IngestCredentialStore(state_dir)
            session_id = "11111111-1111-4111-8111-111111111111"
            _record, secret = store.issue(
                session_id=session_id,
                user_id="user-1",
                scope="INGEST",
                protocols=["rtmp"],
            )
            with self.assertRaises(HTTPException) as failure:
                self._authorize(
                    store,
                    MediaMTXAuthRequest(
                        user=session_id,
                        password=secret,
                        action="read",
                        path="output/relay",
                        protocol="rtmp",
                    ),
                    state_dir,
                )
        self.assertEqual(failure.exception.status_code, 401)

    def test_relay_credential_is_rejected_for_direct_push_session(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            store = IngestCredentialStore(state_dir)
            session_id = "11111111-1111-4111-8111-111111111111"
            _record, secret = store.issue(
                session_id=session_id,
                user_id="user-1",
                scope="RELAY_CLIENT",
                protocols=["rtmp"],
            )
            with self.assertRaises(HTTPException) as failure:
                self._authorize(
                    store,
                    MediaMTXAuthRequest(
                        user=session_id,
                        password=secret,
                        action="read",
                        path="output/relay",
                        protocol="rtmp",
                    ),
                    state_dir,
                    egress_mode="DIRECT_PUSH",
                )
        self.assertEqual(failure.exception.status_code, 401)


class TerminalCredentialRevocationTest(unittest.TestCase):
    def _prepare(self, store: SessionStore, provider: FakeProvider) -> str:
        session_id = str(uuid.uuid4())
        ProvisioningWorkflow(store, provider, WorkflowConfig()).prepare(
            session_id,
            user_id=str(uuid.uuid4()),
            environment="dev",
        )
        return session_id

    def _issue(self, store: IngestCredentialStore, session_id: str) -> list[str]:
        secrets = []
        for scope in ("INGEST", "RELAY_CLIENT"):
            _record, secret = store.issue(
                session_id=session_id,
                user_id=str(uuid.uuid4()),
                scope=scope,
                protocols=["rtmp"],
            )
            secrets.append(secret)
        return secrets

    def test_deadline_stop_revokes_every_scope(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            store = SessionStore(state_dir)
            credential_store = IngestCredentialStore(state_dir)
            provider = FakeProvider()
            session_id = self._prepare(store, provider)
            secrets = self._issue(credential_store, session_id)
            with patch(
                "reaper.default_ingest_store",
                return_value=credential_store,
            ):
                Reaper(
                    store,
                    provider,
                    ReaperConfig(no_ingest_timeout_seconds=0),
                    node_state_path=Path(state_dir) / "missing-nodes.json",
                ).run()

            session = store.get(session_id)
            self.assertEqual(session["status"], "FINISHED")
            for scope, secret in zip(("INGEST", "RELAY_CLIENT"), secrets):
                self.assertIsNone(
                    credential_store.active_for_session(session_id, scope=scope)
                )
                self.assertIsNone(
                    credential_store.verify(
                        username=session_id,
                        secret=secret,
                        protocol="rtmp",
                        scope=scope,
                    )
                )

    def test_stale_heartbeat_revokes_before_failed_cleanup_completes(self) -> None:
        class FailingProvider(FakeProvider):
            def delete_server(self, server_id: str) -> None:
                raise RuntimeError("injected delete failure")

        with tempfile.TemporaryDirectory() as state_dir:
            store = SessionStore(state_dir)
            credential_store = IngestCredentialStore(state_dir)
            provider = FailingProvider()
            session_id = self._prepare(store, provider)
            secrets = self._issue(credential_store, session_id)
            session = store.get(session_id)
            node_state_path = Path(state_dir) / "nodes.json"
            node_state_path.write_text(
                json.dumps(
                    {
                        "nodes": {
                            "node-stale": {
                                "session_id": session_id,
                                "last_heartbeat_at": 0,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            store.bind_node(
                session_id,
                node_id="node-stale",
                boot_id="boot-test",
                provider_server_id=str(session["provider_server_id"]),
                registered_at=0,
            )

            with patch(
                "reaper.default_ingest_store",
                return_value=credential_store,
            ):
                Reaper(
                    store,
                    provider,
                    ReaperConfig(heartbeat_grace_seconds=1),
                    node_state_path=node_state_path,
                ).run()

            current = store.get(session_id)
            self.assertEqual(current["status"], "FAILED_CLEANUP")
            for scope, secret in zip(("INGEST", "RELAY_CLIENT"), secrets):
                self.assertIsNone(
                    credential_store.active_for_session(session_id, scope=scope)
                )
                self.assertIsNone(
                    credential_store.verify(
                        username=session_id,
                        secret=secret,
                        protocol="rtmp",
                        scope=scope,
                    )
                )


if __name__ == "__main__":
    unittest.main()
