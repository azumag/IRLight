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
from ingest_auth_guard import IngestAuthGuard, IngestAuthGuardConfig  # noqa: E402
from ingest_store import IngestCredentialStore  # noqa: E402


class _SessionStore:
    def __init__(self, session: dict[str, object] | None) -> None:
        self.session = session

    def get(self, session_id: str):
        if self.session is not None and self.session.get("session_id") == session_id:
            return dict(self.session)
        return None


def _config(
    *,
    max_ip: int = 10,
    max_credential: int = 3,
    window: float = 60.0,
    lockout: float = 30.0,
    event_limit: int = 20,
    blocked_event_interval: float = 5.0,
) -> IngestAuthGuardConfig:
    return IngestAuthGuardConfig(
        enabled=True,
        failure_window_seconds=window,
        max_failures_per_ip=max_ip,
        max_failures_per_credential=max_credential,
        lockout_seconds=lockout,
        event_limit=event_limit,
        bucket_limit=64,
        blocked_event_interval_seconds=blocked_event_interval,
    )


class IngestAuthGuardTest(unittest.TestCase):
    def test_credential_lockout_expires_with_a_fresh_failure_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            guard = IngestAuthGuard(tmp, config=_config(max_credential=3, lockout=30.0))
            for now in (100.0, 101.0):
                decision = guard.record_failure(
                    source_ip="203.0.113.10",
                    username="candidate-user",
                    protocol="rtmp",
                    now=now,
                )
                self.assertFalse(decision.blocked)

            decision = guard.record_failure(
                source_ip="203.0.113.10",
                username="candidate-user",
                protocol="rtmp",
                now=102.0,
            )
            self.assertTrue(decision.blocked)
            self.assertIn("credential", decision.locked_scopes)
            self.assertEqual(decision.retry_after_seconds, 30)
            self.assertTrue(
                guard.check(
                    source_ip="198.51.100.20", username="candidate-user", now=120.0
                ).blocked
            )

            self.assertFalse(
                guard.check(
                    source_ip="198.51.100.20", username="candidate-user", now=133.0
                ).blocked
            )
            after_expiry = guard.record_failure(
                source_ip="198.51.100.20",
                username="candidate-user",
                protocol="rtmp",
                now=134.0,
            )
            self.assertFalse(after_expiry.blocked)

    def test_ip_lockout_catches_credential_spray(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            guard = IngestAuthGuard(tmp, config=_config(max_ip=3, max_credential=5))
            for index in range(2):
                decision = guard.record_failure(
                    source_ip="203.0.113.11",
                    username=f"guessed-{index}",
                    protocol="srt",
                    now=10.0 + index,
                )
                self.assertFalse(decision.blocked)

            decision = guard.record_failure(
                source_ip="203.0.113.11",
                username="guessed-2",
                protocol="srt",
                now=12.0,
            )
            self.assertTrue(decision.blocked)
            self.assertEqual(decision.locked_scopes, ("ip",))
            self.assertTrue(
                guard.check(
                    source_ip="203.0.113.11", username="completely-new", now=13.0
                ).blocked
            )
            self.assertFalse(
                guard.check(
                    source_ip="203.0.113.12", username="completely-new", now=13.0
                ).blocked
            )

    def test_success_clears_credential_failures_but_keeps_ip_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            guard = IngestAuthGuard(tmp, config=_config(max_ip=10, max_credential=3))
            for now in (100.0, 101.0):
                guard.record_failure(
                    source_ip="203.0.113.12",
                    username="candidate-user",
                    protocol="rtmp",
                    now=now,
                )
            guard.record_success(username="candidate-user", now=102.0)
            decision = guard.record_failure(
                source_ip="203.0.113.12",
                username="candidate-user",
                protocol="rtmp",
                now=103.0,
            )
            self.assertFalse(decision.blocked)

            snapshot = guard.snapshot()
            credential_buckets = [
                bucket
                for key, bucket in snapshot["buckets"].items()
                if key.startswith("credential:")
            ]
            ip_buckets = [
                bucket for key, bucket in snapshot["buckets"].items() if key.startswith("ip:")
            ]
            self.assertEqual(len(credential_buckets), 1)
            self.assertEqual(len(credential_buckets[0]["failures"]), 1)
            self.assertEqual(len(ip_buckets), 1)
            self.assertEqual(len(ip_buckets[0]["failures"]), 3)

    def test_blocked_audit_events_are_throttled_during_lockout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            guard = IngestAuthGuard(
                tmp,
                config=_config(
                    max_ip=10,
                    max_credential=2,
                    lockout=30.0,
                    blocked_event_interval=5.0,
                ),
            )
            for now in (100.0, 101.0):
                guard.record_failure(
                    source_ip="203.0.113.15",
                    username="candidate-user",
                    protocol="rtmp",
                    now=now,
                )

            self.assertTrue(
                guard.record_blocked(
                    source_ip="203.0.113.15",
                    username="candidate-user",
                    protocol="rtmp",
                    now=102.0,
                ).blocked
            )
            guard.record_blocked(
                source_ip="203.0.113.15",
                username="candidate-user",
                protocol="rtmp",
                now=103.0,
            )
            guard.record_blocked(
                source_ip="203.0.113.15",
                username="candidate-user",
                protocol="rtmp",
                now=106.9,
            )
            first_snapshot = guard.snapshot()
            first_blocked = [
                event
                for event in first_snapshot["events"]
                if event.get("type") == "ingest.auth_blocked"
            ]
            self.assertEqual(len(first_blocked), 1)

            guard.record_blocked(
                source_ip="203.0.113.15",
                username="candidate-user",
                protocol="rtmp",
                now=107.0,
            )
            second_blocked = [
                event
                for event in guard.snapshot()["events"]
                if event.get("type") == "ingest.auth_blocked"
            ]
            self.assertEqual(len(second_blocked), 2)

    def test_persisted_audit_is_bounded_and_does_not_store_attacker_username(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            guard = IngestAuthGuard(tmp, config=_config(event_limit=3, max_credential=2))
            for index in range(4):
                guard.record_failure(
                    source_ip="203.0.113.13",
                    username="attacker-controlled-username",
                    protocol="rtmp",
                    publisher_id=f"publisher-{index}",
                    now=200.0 + index,
                )

            raw = Path(tmp, "ingest_auth_guard.json").read_text(encoding="utf-8")
            self.assertNotIn("attacker-controlled-username", raw)
            persisted = json.loads(raw)
            self.assertLessEqual(len(persisted["events"]), 3)
            for event in persisted["events"]:
                payload = event["payload"]
                self.assertNotIn("password", payload)
                self.assertNotIn("token", payload)
                self.assertNotIn("query", payload)
                self.assertNotIn("userAgent", payload)
                self.assertIsNotNone(payload.get("credential_fingerprint"))


class MediaMTXAuthAbuseTest(unittest.TestCase):
    def test_endpoint_locks_repeated_wrong_secret_and_blocks_valid_secret_during_lockout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            credential_store = IngestCredentialStore(tmp)
            guard = IngestAuthGuard(
                tmp,
                config=_config(max_ip=10, max_credential=2, lockout=120.0),
            )
            session_id = "11111111-1111-4111-8111-111111111111"
            _record, secret = credential_store.issue(
                session_id=session_id,
                user_id="deadbeef",
                protocols=["rtmp"],
            )
            session_store = _SessionStore(
                {"session_id": session_id, "user_id": "deadbeef", "status": "READY_WAIT_INGEST"}
            )
            request = MediaMTXAuthRequest(
                user=session_id,
                password="wrong-secret",
                ip="203.0.113.14",
                action="publish",
                path="live/input",
                protocol="rtmp",
                id="publisher-1",
            )

            patches = (
                patch("ingest_api.default_store", return_value=session_store),
                patch("ingest_api.default_ingest_store", return_value=credential_store),
                patch("ingest_api.default_ingest_auth_guard", return_value=guard),
            )
            with patches[0], patches[1], patches[2]:
                with self.assertRaises(HTTPException) as first:
                    authorize_mediamtx_publish(request)
                self.assertEqual(first.exception.status_code, 401)

                with self.assertRaises(HTTPException) as second:
                    authorize_mediamtx_publish(request)
                self.assertEqual(second.exception.status_code, 429)
                self.assertEqual(
                    second.exception.detail, "ingest authentication temporarily blocked"
                )
                self.assertIn("Retry-After", second.exception.headers or {})

                request.password = secret
                with self.assertRaises(HTTPException) as valid_but_locked:
                    authorize_mediamtx_publish(request)
                self.assertEqual(valid_but_locked.exception.status_code, 429)

            persisted = Path(tmp, "ingest_auth_guard.json").read_text(encoding="utf-8")
            self.assertNotIn("wrong-secret", persisted)
            self.assertNotIn(secret, persisted)
            event_types = [event["type"] for event in guard.snapshot()["events"]]
            self.assertIn("ingest.auth_failed", event_types)
            self.assertIn("ingest.auth_locked", event_types)
            self.assertIn("ingest.auth_blocked", event_types)


if __name__ == "__main__":
    unittest.main()
