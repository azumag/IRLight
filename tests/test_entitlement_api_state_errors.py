from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

import session_api  # noqa: E402
from entitlement_store import EntitlementStateError  # noqa: E402


class _PrepareStore:
    def __init__(self) -> None:
        self.begin_prepare_called = False

    def get_prepare_replay(self, _session_id: str, **_kwargs) -> None:
        return None

    def begin_prepare(self, _session_id: str, **_kwargs):
        self.begin_prepare_called = True
        raise AssertionError("begin_prepare must not run when entitlement authority is unavailable")


class _BrokenEntitlementStore:
    def get(self, _user_id: str):
        raise EntitlementStateError(
            "cannot read entitlement state /state/entitlements.json: internal-detail"
        )


class EntitlementApiStateErrorTest(unittest.TestCase):
    def _assert_unavailable(self, entitlement_factory_patch) -> None:
        store = _PrepareStore()
        with patch("session_api.default_store", return_value=store), patch(
            "session_api._validated_destination", return_value=None
        ), entitlement_factory_patch, patch("session_api.default_provider") as provider_factory:
            with self.assertRaises(HTTPException) as failure:
                session_api.prepare_session(
                    "session-1",
                    session_api.PrepareRequest(environment="dev"),
                    {"id": "user-1"},
                    idempotency_key="entitlement-state-error",
                    _csrf=None,
                )

        self.assertEqual(failure.exception.status_code, 503)
        self.assertEqual(
            failure.exception.detail,
            {"code": session_api.ENTITLEMENT_STATE_UNAVAILABLE_CODE},
        )
        self.assertNotIn("/state/", str(failure.exception.detail))
        self.assertNotIn("internal-detail", str(failure.exception.detail))
        self.assertFalse(store.begin_prepare_called)
        provider_factory.assert_not_called()

    def test_entitlement_store_initialization_failure_is_stable_503(self) -> None:
        self._assert_unavailable(
            patch(
                "session_api.default_entitlement_store",
                side_effect=EntitlementStateError(
                    "cannot lock entitlement state /state/entitlements.json"
                ),
            )
        )

    def test_entitlement_store_read_failure_is_stable_503(self) -> None:
        self._assert_unavailable(
            patch(
                "session_api.default_entitlement_store",
                return_value=_BrokenEntitlementStore(),
            )
        )


if __name__ == "__main__":
    unittest.main()
