from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "continuity"))

from make_default_standby import write_png  # noqa: E402
import standby_asset  # noqa: E402
from standby_asset import gst_standby_source, resolve_standby_asset  # noqa: E402


class StandbyAssetPinnedFdTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fallback = self.root / "default.png"
        write_png(self.fallback, width=16, height=9)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_decode_source_stays_bound_to_validated_inode_after_path_replacement(
        self,
    ) -> None:
        custom = self.root / "custom.png"
        write_png(custom, width=16, height=9)
        selection = resolve_standby_asset(str(custom), str(self.fallback))
        try:
            self.assertEqual(selection.source, "CUSTOM")
            self.assertIsNotNone(selection._pinned_fd)
            self.assertIsNotNone(selection._pinned_identity)
            assert selection._pinned_fd is not None
            assert selection._pinned_identity is not None

            validated_identity = selection._pinned_identity
            self.assertEqual(
                (os.fstat(selection._pinned_fd).st_dev, os.fstat(selection._pinned_fd).st_ino),
                validated_identity,
            )

            custom.unlink()
            write_png(custom, width=8, height=8)
            replacement = os.stat(custom)
            self.assertNotEqual(
                (replacement.st_dev, replacement.st_ino),
                validated_identity,
            )

            source = gst_standby_source(selection)
            self.assertIn("uridecodebin", source)
            self.assertNotIn(custom.as_uri(), source)

            aliases = [
                Path("/proc/self/fd") / str(selection._pinned_fd),
                Path("/dev/fd") / str(selection._pinned_fd),
            ]
            matching_aliases: list[Path] = []
            for alias in aliases:
                try:
                    opened = os.stat(alias)
                except OSError:
                    continue
                if (opened.st_dev, opened.st_ino) == validated_identity:
                    matching_aliases.append(alias)

            self.assertTrue(matching_aliases)
            self.assertTrue(
                any(alias.as_uri() in source for alias in matching_aliases)
            )
        finally:
            selection.close()

    def test_custom_without_decoder_handoff_falls_back_to_node_default(self) -> None:
        custom = self.root / "custom.png"
        write_png(custom, width=16, height=9)
        real_fd_uri = standby_asset._pinned_fd_uri_for
        calls = 0

        def selective_fd_uri(fd: int, identity: tuple[int, int]) -> str | None:
            nonlocal calls
            calls += 1
            if calls == 1:
                return None
            return real_fd_uri(fd, identity)

        with mock.patch.object(
            standby_asset, "_pinned_fd_uri_for", side_effect=selective_fd_uri
        ):
            selection = resolve_standby_asset(str(custom), str(self.fallback))

        try:
            self.assertEqual(selection.source, "NODE_DEFAULT")
            self.assertEqual(selection.fallback_reason, "ASSET_UNAVAILABLE")
            self.assertTrue(selection.custom_configured)
            self.assertEqual(selection.path, self.fallback)
            self.assertIn("uridecodebin", gst_standby_source(selection))
            self.assertGreaterEqual(calls, 2)
        finally:
            selection.close()

    def test_no_decoder_handoff_fails_closed_to_synthetic_black(self) -> None:
        custom = self.root / "custom.png"
        write_png(custom, width=16, height=9)

        with mock.patch.object(standby_asset, "_pinned_fd_uri_for", return_value=None):
            selection = resolve_standby_asset(str(custom), str(self.fallback))

        self.assertEqual(selection.source, "SYNTHETIC_BLACK")
        self.assertEqual(
            selection.fallback_reason, "ASSET_AND_NODE_DEFAULT_UNAVAILABLE"
        )
        self.assertTrue(selection.custom_configured)
        self.assertIsNone(selection.path)
        self.assertIsNone(selection._pinned_fd)
        self.assertIn("videotestsrc", gst_standby_source(selection))

    def test_closed_selection_fails_closed_to_synthetic_black(self) -> None:
        custom = self.root / "custom.png"
        write_png(custom, width=16, height=9)
        selection = resolve_standby_asset(str(custom), str(self.fallback))
        self.assertEqual(selection.source, "CUSTOM")

        selection.close()

        source = gst_standby_source(selection)
        self.assertIn("videotestsrc", source)
        self.assertNotIn("uridecodebin", source)


if __name__ == "__main__":
    unittest.main()
