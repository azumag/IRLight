from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "provider"))
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from fastapi import HTTPException  # noqa: E402
import session_api  # noqa: E402
from session_store import (  # noqa: E402
    MediaControlIdempotencyConflict,
    MediaControlNotRunning,
    MediaControlVersionConflict,
    SessionStateError,
    SessionStore,
    new_session,
)


def _store_with_session(
    state_dir: str,
    *,
    status: str = "LIVE",
    mutate_record=None,
) -> tuple[SessionStore, str]:
    record = new_session(user_id="user-1", environment="dev")
    record["session_id"] = "session-1"
    record["status"] = status
    if mutate_record is not None:
        mutate_record(record)
    Path(state_dir, "sessions.json").write_text(
        json.dumps(
            {"sessions": {"session-1": record}, "orphan_cleanup_leases": {}}
        ),
        encoding="utf-8",
    )
    return SessionStore(state_dir), "session-1"


class SessionMediaControlStoreTest(unittest.TestCase):
    def test_new_session_has_live_desired_and_actual(self) -> None:
        record = new_session(user_id="user-1", environment="dev")
        self.assertEqual(
            record["media_control"],
            {
                "audio": {
                    "desired_mode": "LIVE",
                    "actual_mode": "LIVE",
                    "version": 0,
                    "command_id": None,
                    "idempotency_key": None,
                    "updated_at": record["media_control"]["audio"]["updated_at"],
                }
            },
        )

    def test_accepted_command_bumps_version_and_marks_actual_applying(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            store, session_id = _store_with_session(state_dir)
            result = store.set_audio_desired(
                session_id,
                mode="MUTED",
                expected_version=0,
                idempotency_key="key-1",
                operator="user-1",
                occurred_at=1700000000.0,
            )

        self.assertEqual(result["desired_mode"], "MUTED")
        self.assertEqual(result["actual_mode"], "APPLYING")
        self.assertEqual(result["version"], 1)
        self.assertIsInstance(result["command_id"], str)
        self.assertTrue(result["command_id"])

    def test_accepted_command_records_requested_event(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            store, session_id = _store_with_session(state_dir)
            result = store.set_audio_desired(
                session_id,
                mode="MUTED",
                expected_version=0,
                idempotency_key="key-1",
                operator="user-1",
                occurred_at=1700000000.0,
            )
            events = store.get(session_id)["events"]

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "media_control.audio.requested")
        self.assertEqual(events[0]["origin"], "control-api")
        self.assertEqual(
            events[0]["payload"],
            {
                "command_id": result["command_id"],
                "desired_mode": "MUTED",
                "version": 1,
                "operator_user_id": "user-1",
            },
        )

    def test_replayed_idempotency_key_returns_recorded_response(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            store, session_id = _store_with_session(state_dir)
            first = store.set_audio_desired(
                session_id,
                mode="MUTED",
                expected_version=0,
                idempotency_key="key-1",
                operator="user-1",
                occurred_at=1700000000.0,
            )
            replayed = store.set_audio_desired(
                session_id,
                mode="MUTED",
                expected_version=0,
                idempotency_key="key-1",
                operator="user-1",
                occurred_at=1700000001.0,
            )
            events = store.get(session_id)["events"]

        self.assertEqual(replayed, first)
        self.assertEqual(replayed["version"], 1)
        self.assertEqual(len(events), 1)

    def test_same_key_with_different_mode_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            store, session_id = _store_with_session(state_dir)
            store.set_audio_desired(
                session_id,
                mode="MUTED",
                expected_version=0,
                idempotency_key="key-1",
                operator="user-1",
                occurred_at=1700000000.0,
            )
            with self.assertRaises(MediaControlIdempotencyConflict):
                store.set_audio_desired(
                    session_id,
                    mode="LIVE",
                    expected_version=1,
                    idempotency_key="key-1",
                    operator="user-1",
                    occurred_at=1700000001.0,
                )
            session = store.get(session_id)

        self.assertEqual(session["media_control"]["audio"]["version"], 1)

    def test_stale_expected_version_conflicts_with_current_state(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            store, session_id = _store_with_session(state_dir)
            store.set_audio_desired(
                session_id,
                mode="MUTED",
                expected_version=0,
                idempotency_key="key-1",
                operator="user-1",
                occurred_at=1700000000.0,
            )
            with self.assertRaises(MediaControlVersionConflict) as raised:
                store.set_audio_desired(
                    session_id,
                    mode="LIVE",
                    expected_version=0,
                    idempotency_key="key-2",
                    operator="user-1",
                    occurred_at=1700000001.0,
                )

        self.assertEqual(raised.exception.current["version"], 1)
        self.assertEqual(raised.exception.current["desired_mode"], "MUTED")
        self.assertEqual(raised.exception.current["actual_mode"], "APPLYING")

    def test_terminal_session_rejects_command(self) -> None:
        for status in ("STOPPED", "FINISHED", "FAILED"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as state_dir:
                store, session_id = _store_with_session(state_dir, status=status)
                with self.assertRaises(MediaControlNotRunning):
                    store.set_audio_desired(
                        session_id,
                        mode="MUTED",
                        expected_version=0,
                        idempotency_key="key-1",
                        operator="user-1",
                        occurred_at=1700000000.0,
                    )

    def test_desired_matching_last_actual_keeps_actual(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            store, session_id = _store_with_session(state_dir)
            result = store.set_audio_desired(
                session_id,
                mode="LIVE",
                expected_version=0,
                idempotency_key="key-1",
                operator="user-1",
                occurred_at=1700000000.0,
            )

        self.assertEqual(result["desired_mode"], "LIVE")
        self.assertEqual(result["actual_mode"], "LIVE")
        self.assertEqual(result["version"], 1)

    def test_switch_back_to_live_while_apply_pending_stays_applying(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            store, session_id = _store_with_session(state_dir)
            store.set_audio_desired(
                session_id,
                mode="MUTED",
                expected_version=0,
                idempotency_key="key-1",
                operator="user-1",
                occurred_at=1700000000.0,
            )
            result = store.set_audio_desired(
                session_id,
                mode="LIVE",
                expected_version=1,
                idempotency_key="key-2",
                operator="user-1",
                occurred_at=1700000001.0,
            )

        self.assertEqual(result["desired_mode"], "LIVE")
        self.assertEqual(result["actual_mode"], "APPLYING")
        self.assertEqual(result["version"], 2)

    def test_state_survives_store_reload(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            store, session_id = _store_with_session(state_dir)
            store.set_audio_desired(
                session_id,
                mode="MUTED",
                expected_version=0,
                idempotency_key="key-1",
                operator="user-1",
                occurred_at=1700000000.0,
            )
            reloaded = SessionStore(state_dir)
            audio = reloaded.get(session_id)["media_control"]["audio"]

        self.assertEqual(audio["desired_mode"], "MUTED")
        self.assertEqual(audio["actual_mode"], "APPLYING")
        self.assertEqual(audio["version"], 1)
        self.assertEqual(audio["idempotency_key"], "key-1")

    def test_legacy_record_without_media_control_is_backfilled(self) -> None:
        def strip(record: dict[str, object]) -> None:
            del record["media_control"]

        with tempfile.TemporaryDirectory() as state_dir:
            store, session_id = _store_with_session(
                state_dir, mutate_record=strip
            )
            result = store.set_audio_desired(
                session_id,
                mode="MUTED",
                expected_version=0,
                idempotency_key="key-1",
                operator="user-1",
                occurred_at=1700000000.0,
            )

        self.assertEqual(result["desired_mode"], "MUTED")
        self.assertEqual(result["version"], 1)

    def test_malformed_persisted_media_control_fails_closed(self) -> None:
        malformed_records = (
            {"audio": "MUTED"},
            {"audio": {"desired_mode": "LOUD", "actual_mode": "LIVE", "version": 0}},
            {"audio": {"desired_mode": "LIVE", "actual_mode": "LOUD", "version": 0}},
            {"audio": {"desired_mode": "LIVE", "actual_mode": "LIVE", "version": -1}},
            {
                "audio": {
                    "desired_mode": "LIVE",
                    "actual_mode": "LIVE",
                    "version": 0,
                    "updated_at": float("nan"),
                }
            },
            {
                "audio": {
                    "desired_mode": "LIVE",
                    "actual_mode": "LIVE",
                    "version": 0,
                    "idempotency_key": "k" * 201,
                    "updated_at": 1.0,
                }
            },
        )
        for media_control in malformed_records:
            with self.subTest(media_control=media_control):
                def corrupt(record: dict[str, object]) -> None:
                    record["media_control"] = media_control

                with tempfile.TemporaryDirectory() as state_dir:
                    with self.assertRaises(SessionStateError):
                        _store_with_session(state_dir, mutate_record=corrupt)

    def test_invalid_arguments_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            store, session_id = _store_with_session(state_dir)
            for kwargs in (
                {"mode": "LOUD"},
                {"expected_version": -1},
                {"expected_version": 1.5},
                {"idempotency_key": ""},
                {"idempotency_key": "k" * 201},
            ):
                with self.subTest(kwargs=kwargs):
                    call = {
                        "mode": "MUTED",
                        "expected_version": 0,
                        "idempotency_key": "key-1",
                        "operator": "user-1",
                        "occurred_at": 1700000000.0,
                    }
                    call.update(kwargs)
                    with self.assertRaises(ValueError):
                        store.set_audio_desired(session_id, **call)


class SessionMediaControlApiTest(unittest.TestCase):
    def _command(self, **kwargs) -> session_api.MediaControlAudioRequest:
        fields = {"mode": "MUTED", "expected_version": 0}
        fields.update(kwargs)
        return session_api.MediaControlAudioRequest(**fields)

    def test_missing_idempotency_key_is_rejected(self) -> None:
        with (
            patch("session_api._owned_session", return_value={"session_id": "s"}),
            self.assertRaises(HTTPException) as raised,
        ):
            session_api.set_session_audio(
                "s", self._command(), {"id": "owner"}, None, None
            )

        self.assertEqual(raised.exception.status_code, 400)

    def test_version_conflict_maps_to_409_with_current_state(self) -> None:
        store = Mock()
        store.set_audio_desired.side_effect = MediaControlVersionConflict(
            {
                "desired_mode": "MUTED",
                "actual_mode": "APPLYING",
                "version": 3,
                "command_id": "cmd-1",
            }
        )
        with (
            patch("session_api._owned_session", return_value={"session_id": "s"}),
            patch("session_api._session_store", return_value=store),
            self.assertRaises(HTTPException) as raised,
        ):
            session_api.set_session_audio(
                "s", self._command(), {"id": "owner"}, None, "key-1"
            )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            raised.exception.detail,
            {
                "code": "VERSION_CONFLICT",
                "current_version": 3,
                "current_mode": "MUTED",
            },
        )

    def test_not_running_maps_to_409(self) -> None:
        store = Mock()
        store.set_audio_desired.side_effect = MediaControlNotRunning()
        with (
            patch("session_api._owned_session", return_value={"session_id": "s"}),
            patch("session_api._session_store", return_value=store),
            self.assertRaises(HTTPException) as raised,
        ):
            session_api.set_session_audio(
                "s", self._command(), {"id": "owner"}, None, "key-1"
            )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail, {"code": "SESSION_NOT_RUNNING"})

    def test_idempotency_conflict_maps_to_409(self) -> None:
        store = Mock()
        store.set_audio_desired.side_effect = MediaControlIdempotencyConflict()
        with (
            patch("session_api._owned_session", return_value={"session_id": "s"}),
            patch("session_api._session_store", return_value=store),
            self.assertRaises(HTTPException) as raised,
        ):
            session_api.set_session_audio(
                "s", self._command(), {"id": "owner"}, None, "key-1"
            )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail, {"code": "IDEMPOTENCY_CONFLICT"})

    def test_valid_command_reaches_store(self) -> None:
        store = Mock()
        store.set_audio_desired.return_value = {
            "desired_mode": "MUTED",
            "actual_mode": "APPLYING",
            "version": 1,
            "command_id": "cmd-1",
        }
        with (
            patch("session_api._owned_session", return_value={"session_id": "s"}),
            patch("session_api._session_store", return_value=store),
        ):
            result = session_api.set_session_audio(
                "s", self._command(), {"id": "owner"}, None, "key-1"
            )

        self.assertEqual(result["desired_mode"], "MUTED")
        store.set_audio_desired.assert_called_once_with(
            "s",
            mode="MUTED",
            expected_version=0,
            idempotency_key="key-1",
            operator="owner",
        )


if __name__ == "__main__":
    unittest.main()
