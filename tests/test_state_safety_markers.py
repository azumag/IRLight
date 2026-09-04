from __future__ import annotations

import concurrent.futures
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

import state_safety  # noqa: E402


class InitializationMarkerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "authority.json"
        self.marker = state_safety.initialization_marker(self.path)

    def _observe_sync(self, fail_kind: str | None = None):
        actual_sync = os.fsync
        calls: list[str] = []

        def sync(fd: int) -> None:
            kind = "directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
            calls.append(kind)
            if kind == fail_kind:
                raise OSError("injected fsync failure")
            actual_sync(fd)

        return calls, sync

    def test_new_marker_is_private_and_durable(self) -> None:
        self.assertFalse(state_safety.was_initialized(self.path))
        calls, sync = self._observe_sync()
        with mock.patch.object(state_safety.os, "fsync", side_effect=sync):
            state_safety.mark_initialized(self.path)
        self.assertEqual(calls, ["file", "directory"])
        self.assertEqual(stat.S_IMODE(self.marker.stat().st_mode), 0o600)
        self.assertEqual(self.marker.read_bytes(), b"v1\n")
        self.assertTrue(state_safety.was_initialized(self.path))
        self.assertFalse(self.path.exists())

    def test_retry_after_marker_fsync_failure_reestablishes_durability(self) -> None:
        _, failing_sync = self._observe_sync("file")
        with mock.patch.object(state_safety.os, "fsync", side_effect=failing_sync):
            with self.assertRaises(OSError):
                state_safety.mark_initialized(self.path)
        self.assertTrue(self.marker.exists())
        calls, sync = self._observe_sync()
        with mock.patch.object(state_safety.os, "fsync", side_effect=sync):
            state_safety.mark_initialized(self.path)
        self.assertEqual(calls, ["file", "directory"])

    def test_retry_after_directory_fsync_failure_reestablishes_durability(self) -> None:
        _, failing_sync = self._observe_sync("directory")
        with mock.patch.object(state_safety.os, "fsync", side_effect=failing_sync):
            with self.assertRaises(OSError):
                state_safety.mark_initialized(self.path)
        calls, sync = self._observe_sync()
        with mock.patch.object(state_safety.os, "fsync", side_effect=sync):
            state_safety.mark_initialized(self.path)
        self.assertEqual(calls, ["file", "directory"])

    def test_existing_marker_is_not_truncated(self) -> None:
        self.marker.write_bytes(b"existing fuse\n")
        state_safety.mark_initialized(self.path)
        self.assertEqual(self.marker.read_bytes(), b"existing fuse\n")

    def test_empty_marker_still_counts_as_initialization(self) -> None:
        self.marker.touch(mode=0o600)
        self.assertTrue(state_safety.was_initialized(self.path))
        calls, sync = self._observe_sync()
        with mock.patch.object(state_safety.os, "fsync", side_effect=sync):
            state_safety.mark_initialized(self.path)
        self.assertEqual(calls, ["file", "directory"])
        self.assertEqual(self.marker.read_bytes(), b"")

    def test_dangling_symlink_cannot_look_uninitialized(self) -> None:
        self.marker.symlink_to(self.path.parent / "missing")
        self.assertTrue(state_safety.was_initialized(self.path))
        with self.assertRaises(OSError):
            state_safety.mark_initialized(self.path)

    def test_symlink_target_is_not_modified(self) -> None:
        target = self.path.parent / "untouched"
        target.write_bytes(b"do not modify")
        self.marker.symlink_to(target)
        self.assertTrue(state_safety.was_initialized(self.path))
        with self.assertRaises(OSError):
            state_safety.mark_initialized(self.path)
        self.assertEqual(target.read_bytes(), b"do not modify")

    def test_directory_cannot_look_uninitialized(self) -> None:
        self.marker.mkdir()
        self.assertTrue(state_safety.was_initialized(self.path))
        with self.assertRaises(OSError):
            state_safety.mark_initialized(self.path)

    def test_marker_inspection_failure_propagates(self) -> None:
        with mock.patch.object(Path, "lstat", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                state_safety.was_initialized(self.path)

    def test_concurrent_initializers_preserve_one_fuse(self) -> None:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(state_safety.mark_initialized, self.path) for _ in range(32)]
            for future in futures:
                future.result(timeout=5)
        self.assertEqual(self.marker.read_bytes(), b"v1\n")
        self.assertTrue(state_safety.was_initialized(self.path))


if __name__ == "__main__":
    unittest.main()
