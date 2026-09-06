from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "provider"))
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from fastapi import HTTPException  # noqa: E402
from session_api import SessionEventRequest, add_session_event  # noqa: E402
from session_event_policy import USER_EVENT_MAX_PAYLOAD_BYTES  # noqa: E402


class SessionUserEventApiTest(unittest.TestCase):
    def test_invalid_payload_is_rejected_before_store_append(self) -> None:
        store = Mock()
        request = SessionEventRequest(
            type="user.note",
            payload={"note": "x" * USER_EVENT_MAX_PAYLOAD_BYTES},
        )
        with (
            patch("session_api._owned_session", return_value={"session_id": "s"}),
            patch("session_api._session_store", return_value=store),
            self.assertRaises(HTTPException) as raised,
        ):
            add_session_event("s", request, {"id": "owner"}, None)

        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(
            raised.exception.detail,
            {"code": "USER_EVENT_PAYLOAD_INVALID"},
        )
        store.append_event.assert_not_called()

    def test_valid_payload_reaches_store_without_rewriting_fields(self) -> None:
        store = Mock()
        store.append_event.return_value = {"sequence": 1}
        request = SessionEventRequest(
            type="user.note",
            reason_code="USER_CONTEXT",
            payload={"note": "stream starts soon"},
        )
        with (
            patch("session_api._owned_session", return_value={"session_id": "s"}),
            patch("session_api._session_store", return_value=store),
        ):
            result = add_session_event("s", request, {"id": "owner"}, None)

        self.assertEqual(result, {"sequence": 1})
        store.append_event.assert_called_once_with(
            "s",
            event_type="user.note",
            reason_code="USER_CONTEXT",
            payload={"note": "stream starts soon"},
            origin="user-api",
        )


if __name__ == "__main__":
    unittest.main()
