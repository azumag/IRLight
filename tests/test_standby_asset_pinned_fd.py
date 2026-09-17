from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "continuity"))

from make_default_standby import write_png  # noqa: E402
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
