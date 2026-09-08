from __future__ import annotations

import multiprocessing
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))
sys.path.insert(0, str(ROOT / "apps" / "continuity"))

from control_state import ControlStateReader  # noqa: E402
from control_store import ControlStateError, ControlStore  # noqa: E402


def _concurrent_update(state_dir: str, mode: str, key: str) -> None:
    ControlStore(state_dir).update(mode=mode, idempotency_key=key)


class ControlStoreTest(unittest.TestCase):
    def test_initialized_state_deletion_and_corruption_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            store = ControlStore(state_dir)
            store.ensure()
            store.update(mode="MUTED", idempotency_key="mute")
            store.path.unlink()
            with self.assertRaises(ControlStateError):
                store.get()
            with self.assertRaises(ControlStateError):
                store.ensure()

        with tempfile.TemporaryDirectory() as state_dir:
            store = ControlStore(state_dir)
            store.ensure()
            store.path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(ControlStateError):
                store.get()

    def test_processes_increment_version_without_lost_update(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            store = ControlStore(state_dir)
            store.ensure()
            context = multiprocessing.get_context("fork")
            processes = [
                context.Process(
                    target=_concurrent_update,
                    args=(state_dir, mode, key),
                )
                for mode, key in (("MUTED", "one"), ("LIVE", "two"))
            ]
            for process in processes:
                process.start()
            for process in processes:
                process.join(timeout=10)
                self.assertEqual(process.exitcode, 0)
            self.assertEqual(store.get()["version"], 2)

    def test_continuity_reader_starts_muted_and_preserves_last_valid_command(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            path = Path(state_dir) / "control.json"
            reader = ControlStateReader(path)
            missing, error = reader.read()
            self.assertEqual(missing.audio_mode, "MUTED")
            self.assertEqual(error, "CONTROL_STATE_UNAVAILABLE")

            store = ControlStore(state_dir)
            store.ensure()
            valid, error = reader.read()
            self.assertEqual(valid.audio_mode, "LIVE")
            self.assertIsNone(error)

            store.update(mode="MUTED", idempotency_key="mute")
            muted, error = reader.read()
            self.assertEqual(muted.audio_mode, "MUTED")
            self.assertIsNone(error)

            path.write_text("[]", encoding="utf-8")
            preserved, error = reader.read()
            self.assertEqual(preserved.audio_mode, "MUTED")
            self.assertEqual(error, "CONTROL_STATE_INVALID")

    def test_continuity_reader_rejects_ambiguous_and_overflowing_json(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            store = ControlStore(state_dir)
            store.ensure()
            path = store.path
            reader = ControlStateReader(path)

            valid, error = reader.read()
            self.assertEqual(valid.audio_mode, "LIVE")
            self.assertIsNone(error)

            invalid_payloads = (
                '{"audio_mode":"MUTED","audio_mode":"LIVE","version":1,'
                '"command_id":null,"updated_at":1}',
                '{"audio_mode":"MUTED","version":1,"command_id":null,'
                '"updated_at":NaN}',
                '{"audio_mode":"MUTED","version":1,"command_id":null,'
                f'"updated_at":1{"0" * 400}}}',
                '{"audio_mode":"MUTED","version":1,"command_id":"not-a-uuid",'
                '"idempotency_key":"key","updated_at":1}',
                '{"audio_mode":"MUTED","version":1,"command_id":null,'
                '"idempotency_key":[],"updated_at":1}',
            )
            for payload in invalid_payloads:
                with self.subTest(payload=payload[:80]):
                    path.write_text(payload, encoding="utf-8")
                    preserved, error = reader.read()
                    self.assertEqual(preserved.audio_mode, "LIVE")
                    self.assertEqual(error, "CONTROL_STATE_INVALID")

    def test_non_finite_update_does_not_replace_existing_authority(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            store = ControlStore(state_dir)
            store.ensure()
            before = store.path.read_bytes()

            with self.assertRaises(ControlStateError):
                store.update(
                    mode="MUTED",
                    idempotency_key="non-finite",
                    now=float("nan"),
                )

            self.assertEqual(store.path.read_bytes(), before)
            self.assertEqual(store.get()["audio_mode"], "LIVE")

    def test_timestamp_overflow_is_controlled_and_preserves_authority(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            store = ControlStore(state_dir)
            store.ensure()
            before = store.path.read_bytes()

            with self.assertRaisesRegex(ControlStateError, "invalid update time"):
                store.update(
                    mode="MUTED",
                    idempotency_key="overflow",
                    now=10**400,
                )

            self.assertEqual(store.path.read_bytes(), before)
            self.assertEqual(store.get()["audio_mode"], "LIVE")

    def test_serialization_failure_is_controlled_and_preserves_authority(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            store = ControlStore(state_dir)
            store.ensure()
            before = store.path.read_bytes()

            with mock.patch(
                "control_store.json.dump", side_effect=ValueError("synthetic failure")
            ):
                with self.assertRaisesRegex(
                    ControlStateError, "control state cannot be serialized"
                ):
                    store.update(mode="MUTED", idempotency_key="serialize-failure")

            self.assertEqual(store.path.read_bytes(), before)
            self.assertEqual(store.get()["audio_mode"], "LIVE")


if __name__ == "__main__":
    unittest.main()
