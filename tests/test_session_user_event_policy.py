from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from session_event_policy import (  # noqa: E402
    USER_EVENT_MAX_DEPTH,
    USER_EVENT_MAX_ELEMENTS,
    USER_EVENT_MAX_PAYLOAD_BYTES,
    UserEventPayloadError,
    validate_user_event_payload,
)


class SessionUserEventPolicyTest(unittest.TestCase):
    def test_payload_at_byte_budget_is_allowed(self) -> None:
        overhead = len('{"note":""}'.encode("utf-8"))
        payload = {"note": "x" * (USER_EVENT_MAX_PAYLOAD_BYTES - overhead)}
        validate_user_event_payload(payload)

    def test_payload_above_byte_budget_is_rejected(self) -> None:
        overhead = len('{"note":""}'.encode("utf-8"))
        payload = {"note": "x" * (USER_EVENT_MAX_PAYLOAD_BYTES - overhead + 1)}
        with self.assertRaises(UserEventPayloadError):
            validate_user_event_payload(payload)

    def test_multibyte_text_uses_utf8_byte_budget(self) -> None:
        payload = {"note": "あ" * USER_EVENT_MAX_PAYLOAD_BYTES}
        with self.assertRaises(UserEventPayloadError):
            validate_user_event_payload(payload)

    def test_depth_boundary_is_enforced(self) -> None:
        payload: dict[str, object] = {"value": "ok"}
        for _ in range(USER_EVENT_MAX_DEPTH - 1):
            payload = {"nested": payload}
        validate_user_event_payload(payload)

        too_deep: dict[str, object] = {"nested": payload}
        with self.assertRaises(UserEventPayloadError):
            validate_user_event_payload(too_deep)

    def test_total_element_boundary_is_enforced(self) -> None:
        validate_user_event_payload(
            {str(index): None for index in range(USER_EVENT_MAX_ELEMENTS)}
        )
        with self.assertRaises(UserEventPayloadError):
            validate_user_event_payload(
                {str(index): None for index in range(USER_EVENT_MAX_ELEMENTS + 1)}
            )

    def test_nested_array_elements_count_toward_budget(self) -> None:
        with self.assertRaises(UserEventPayloadError):
            validate_user_event_payload(
                {"items": [None] * USER_EVENT_MAX_ELEMENTS}
            )

    def test_non_finite_numbers_are_rejected(self) -> None:
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value), self.assertRaises(UserEventPayloadError):
                validate_user_event_payload({"value": value})

    def test_non_json_values_are_rejected(self) -> None:
        with self.assertRaises(UserEventPayloadError):
            validate_user_event_payload({"value": object()})


if __name__ == "__main__":
    unittest.main()
