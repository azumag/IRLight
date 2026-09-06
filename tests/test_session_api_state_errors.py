from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

import session_api  # noqa: E402
from session_store import SessionStateError  # noqa: E402


class _EntitlementStore:
    def get(self, user_id: str) -> dict[str, object]:
        return {
            "id": f"user:{user_id}",
            "user_id": user_id,
            "max_concurrent_sessions": 1,
        }


class _ReplayBrokenStore:
    def get_prepare_replay(self, _session_id: str, **_kwargs):
        raise SessionStateError(
            "cannot read Session state /state/sessions.json: internal-detail"
        )


class _BeginBrokenStore:
    def __init__(self) -> None:
        self.begin_prepare_called = False

    def get_prepare_replay(self, _session_id: str, **_kwargs):
        return None

    def begin_prepare(self, _session_id: str, **_kwargs):
        self.begin_prepare_called = True
        raise SessionStateError(
            "cannot read Session state /state/sessions.json: changed-after-read"
        )


class SessionApiStateErrorTest(unittest.TestCase):
    def _assert_stable_unavailable(self, failure: HTTPException) -> None:
        self.assertEqual(failure.status_code, 503)
        self.assertEqual(
            failure.detail,
            {"code": session_api.SESSION_STATE_UNAVAILABLE_CODE},
        )
        self.assertNotIn("/state/", str(failure.detail))
        self.assertNotIn("internal-detail", str(failure.detail))
        self.assertNotIn("changed-after-read", str(failure.detail))

    def test_session_store_initialization_failure_stops_before_provider(self) -> None:
        with patch(
            "session_api.default_store",
            side_effect=SessionStateError(
                "cannot lock Session state /state/sessions.json: internal-detail"
            ),
        ), patch("session_api._validated_destination") as destination, patch(
            "session_api.default_entitlement_store"
        ) as entitlement_factory, patch("session_api.default_provider") as provider_factory:
            with self.assertRaises(HTTPException) as failure:
                session_api.prepare_session(
                    "session-1",
                    session_api.PrepareRequest(environment="dev"),
                    {"id": "user-1"},
                    idempotency_key="session-state-init-error",
                    _csrf=None,
                )

        self._assert_stable_unavailable(failure.exception)
        destination.assert_not_called()
        entitlement_factory.assert_not_called()
        provider_factory.assert_not_called()

    def test_session_replay_read_failure_stops_before_provider(self) -> None:
        with patch("session_api.default_store", return_value=_ReplayBrokenStore()), patch(
            "session_api._validated_destination"
        ) as destination, patch("session_api.default_entitlement_store") as entitlement_factory, patch(
            "session_api.default_provider"
        ) as provider_factory:
            with self.assertRaises(HTTPException) as failure:
                session_api.prepare_session(
                    "session-1",
                    session_api.PrepareRequest(environment="dev"),
                    {"id": "user-1"},
                    idempotency_key="session-state-read-error",
                    _csrf=None,
                )

        self._assert_stable_unavailable(failure.exception)
        destination.assert_not_called()
        entitlement_factory.assert_not_called()
        provider_factory.assert_not_called()

    def test_session_begin_prepare_failure_never_selects_provider(self) -> None:
        store = _BeginBrokenStore()
        with patch("session_api.default_store", return_value=store), patch(
            "session_api._validated_destination", return_value=None
        ), patch(
            "session_api.default_entitlement_store", return_value=_EntitlementStore()
        ), patch("session_api.default_provider") as provider_factory:
            with self.assertRaises(HTTPException) as failure:
                session_api.prepare_session(
                    "session-1",
                    session_api.PrepareRequest(environment="dev"),
                    {"id": "user-1"},
                    idempotency_key="session-state-begin-error",
                    _csrf=None,
                )

        self._assert_stable_unavailable(failure.exception)
        self.assertTrue(store.begin_prepare_called)
        provider_factory.assert_not_called()

    def test_owned_session_read_failure_is_stable_503(self) -> None:
        store = _ReplayBrokenStore()
        store.get = lambda _session_id: (_ for _ in ()).throw(
            SessionStateError("cannot read /state/sessions.json: internal-detail")
        )
        with patch("session_api.default_store", return_value=store):
            with self.assertRaises(HTTPException) as failure:
                session_api.get_session("session-1", {"id": "user-1"})
        self._assert_stable_unavailable(failure.exception)


if __name__ == "__main__":
    unittest.main()
