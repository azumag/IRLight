from __future__ import annotations

import math
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "provider"))
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from fake_provider import FakeProvider  # noqa: E402
from reaper import Reaper, ReaperConfig  # noqa: E402
from session_store import SessionStore  # noqa: E402


class CountingProvider(FakeProvider):
    def __init__(self) -> None:
        super().__init__()
        self.list_calls = 0

    def list_managed_resources(self):
        self.list_calls += 1
        return super().list_managed_resources()


class ReaperClockValidationTest(unittest.TestCase):
    def _store(self) -> SessionStore:
        return SessionStore(tempfile.mkdtemp(prefix="irlight-reaper-clock-"))

    def test_invalid_runtime_clock_fails_before_mutation_or_provider_access(self) -> None:
        invalid_values = (-1.0, math.nan, math.inf, -math.inf, True, 10**10000)

        for invalid in invalid_values:
            with self.subTest(invalid=type(invalid).__name__):
                store = self._store()
                session = store.create(user_id="deadbeef", environment="dev")
                before = store.path.read_bytes()
                provider = CountingProvider()

                with self.assertRaisesRegex(
                    ValueError,
                    "reaper clock must be a finite non-negative number",
                ):
                    Reaper(store, provider, ReaperConfig(), now=invalid).run()

                self.assertEqual(store.path.read_bytes(), before)
                self.assertEqual(provider.list_calls, 0)
                self.assertEqual(provider.list_managed_resources(), [])
                self.assertEqual(store.get(str(session["session_id"]))["status"], "CREATED")

    def test_invalid_default_clock_fails_before_mutation(self) -> None:
        store = self._store()
        store.create(user_id="deadbeef", environment="dev")
        before = store.path.read_bytes()
        provider = CountingProvider()
        reaper = Reaper(store, provider, ReaperConfig())

        with patch("reaper.time.time", return_value=math.nan):
            with self.assertRaisesRegex(
                ValueError,
                "reaper clock must be a finite non-negative number",
            ):
                reaper.run()

        self.assertEqual(store.path.read_bytes(), before)
        self.assertEqual(provider.list_calls, 0)

    def test_invalid_config_duration_is_rejected(self) -> None:
        fields = (
            "provisioning_timeout_seconds",
            "no_ingest_timeout_seconds",
            "hold_timeout_seconds",
            "heartbeat_grace_seconds",
            "orphan_grace_seconds",
        )
        invalid_values = (-1.0, math.nan, math.inf, -math.inf, True, 10**10000)

        for field in fields:
            for invalid in invalid_values:
                with self.subTest(field=field, invalid=type(invalid).__name__):
                    with self.assertRaisesRegex(
                        ValueError,
                        f"reaper {field} must be a finite non-negative number",
                    ):
                        ReaperConfig(**{field: invalid})

    def test_epoch_zero_remains_valid(self) -> None:
        store = self._store()
        store.create(user_id="deadbeef", environment="dev")
        result = Reaper(store, CountingProvider(), ReaperConfig(), now=0.0).run()
        self.assertEqual(result["timeout_failures"], 0)

    def test_timeout_sweep_pins_one_clock_sample_for_generated_events(self) -> None:
        store = self._store()
        session = store.create(
            session_id=str(uuid.uuid4()),
            user_id="deadbeef",
            environment="dev",
        )
        session_id = str(session["session_id"])
        store.transition(
            session_id,
            "PROVISIONING",
            provisioning_started_at=0.0,
        )
        reaper = Reaper(
            store,
            FakeProvider(),
            ReaperConfig(provisioning_timeout_seconds=10.0),
        )

        with patch.object(reaper, "now", side_effect=[100.0, math.nan]) as clock:
            result = reaper.run()

        self.assertEqual(clock.call_count, 1)
        self.assertEqual(result["timeout_failures"], 1)
        current = store.get(session_id)
        self.assertEqual(current["status"], "FAILED")
        failed_event = next(
            event for event in reversed(current["events"])
            if event.get("type") == "session.failed"
        )
        self.assertEqual(failed_event["occurred_at"], 100.0)


if __name__ == "__main__":
    unittest.main()
