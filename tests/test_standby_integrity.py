from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "continuity"))

from make_default_standby import write_png  # noqa: E402
from standby_asset import resolve_standby_asset  # noqa: E402
from standby_integrity import (  # noqa: E402
    resolve_integrity_checked_standby_asset,
    verify_selected_snapshot,
)


class StandbyIntegrityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.custom = self.root / "custom.png"
        self.fallback = self.root / "default.png"
        write_png(self.custom, width=16, height=9)
        write_png(self.fallback, width=8, height=8)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def _digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _resolve(self, **kwargs):
        selection = resolve_integrity_checked_standby_asset(
            str(self.custom),
            str(self.fallback),
            **kwargs,
        )
        self.addCleanup(selection.close)
        return selection

    def test_matching_digest_and_size_keeps_custom(self) -> None:
        selection = self._resolve(
            expected_sha256=self._digest(self.custom),
            expected_size_bytes=str(self.custom.stat().st_size),
        )

        self.assertEqual(selection.source, "CUSTOM")
        self.assertIsNone(selection.fallback_reason)

    def test_wrong_digest_falls_back_to_node_default(self) -> None:
        expected = "0" * 64
        self.assertNotEqual(expected, self._digest(self.custom))

        selection = self._resolve(expected_sha256=expected)

        self.assertEqual(selection.source, "NODE_DEFAULT")
        self.assertEqual(selection.fallback_reason, "ASSET_INTEGRITY_CHECK_FAILED")
        self.assertTrue(selection.custom_configured)

    def test_wrong_size_falls_back_to_node_default(self) -> None:
        selection = self._resolve(
            expected_sha256=self._digest(self.custom),
            expected_size_bytes=str(self.custom.stat().st_size + 1),
        )

        self.assertEqual(selection.source, "NODE_DEFAULT")
        self.assertEqual(selection.fallback_reason, "ASSET_INTEGRITY_CHECK_FAILED")

    def test_size_without_digest_falls_back_to_node_default(self) -> None:
        selection = self._resolve(
            expected_size_bytes=str(self.custom.stat().st_size),
        )

        self.assertEqual(selection.source, "NODE_DEFAULT")
        self.assertEqual(selection.fallback_reason, "ASSET_INTEGRITY_CHECK_FAILED")

    def test_unbounded_size_text_falls_back_without_integer_conversion_failure(self) -> None:
        selection = self._resolve(
            expected_sha256=self._digest(self.custom),
            expected_size_bytes="9" * 5000,
        )

        self.assertEqual(selection.source, "NODE_DEFAULT")
        self.assertEqual(selection.fallback_reason, "ASSET_INTEGRITY_CHECK_FAILED")

    def test_invalid_digest_falls_back_to_node_default(self) -> None:
        selection = self._resolve(expected_sha256="A" * 64)

        self.assertEqual(selection.source, "NODE_DEFAULT")
        self.assertEqual(selection.fallback_reason, "ASSET_INTEGRITY_CHECK_FAILED")

    def test_integrity_failure_without_default_uses_synthetic_black(self) -> None:
        missing_default = self.root / "missing-default.png"
        selection = resolve_integrity_checked_standby_asset(
            str(self.custom),
            str(missing_default),
            expected_sha256="0" * 64,
        )
        self.addCleanup(selection.close)

        self.assertEqual(selection.source, "SYNTHETIC_BLACK")
        self.assertEqual(
            selection.fallback_reason,
            "ASSET_INTEGRITY_CHECK_FAILED_AND_NODE_DEFAULT_UNAVAILABLE",
        )
        self.assertTrue(selection.custom_configured)

    def test_unconfigured_integrity_preserves_legacy_custom_selection(self) -> None:
        selection = self._resolve()

        self.assertEqual(selection.source, "CUSTOM")
        self.assertIsNone(selection.fallback_reason)

    def test_verification_does_not_move_snapshot_offset(self) -> None:
        selection = resolve_standby_asset(str(self.custom), str(self.fallback))
        self.addCleanup(selection.close)
        self.assertEqual(selection.source, "CUSTOM")
        self.assertIsNotNone(selection._pinned_fd)
        assert selection._pinned_fd is not None

        os.lseek(selection._pinned_fd, 3, os.SEEK_SET)
        verify_selected_snapshot(
            selection,
            expected_sha256=self._digest(self.custom),
            expected_size_bytes=str(self.custom.stat().st_size),
        )

        self.assertEqual(os.lseek(selection._pinned_fd, 0, os.SEEK_CUR), 3)


if __name__ == "__main__":
    unittest.main()
