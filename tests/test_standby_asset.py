from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "continuity"))

from make_default_standby import write_png  # noqa: E402
from standby_asset import (  # noqa: E402
    gst_standby_source,
    public_standby_status,
    resolve_standby_asset,
)


class StandbyAssetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fallback = self.root / "default.png"
        write_png(self.fallback, width=16, height=9)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_custom_valid_image_wins(self) -> None:
        custom = self.root / "custom.png"
        write_png(custom, width=16, height=9)
        selection = resolve_standby_asset(str(custom), str(self.fallback))
        self.assertEqual(selection.source, "CUSTOM")
        self.assertEqual(selection.path, custom)
        self.assertIsNone(selection.fallback_reason)

    def test_missing_custom_falls_back_to_node_default(self) -> None:
        selection = resolve_standby_asset(
            str(self.root / "missing.png"), str(self.fallback)
        )
        self.assertEqual(selection.source, "NODE_DEFAULT")
        self.assertEqual(selection.path, self.fallback)
        self.assertEqual(selection.fallback_reason, "ASSET_UNAVAILABLE")
        self.assertTrue(selection.custom_configured)

    def test_unsupported_custom_falls_back_to_node_default(self) -> None:
        custom = self.root / "not-an-image.bin"
        custom.write_text("not an image", encoding="utf-8")
        selection = resolve_standby_asset(str(custom), str(self.fallback))
        self.assertEqual(selection.source, "NODE_DEFAULT")
        self.assertEqual(selection.fallback_reason, "ASSET_UNAVAILABLE")

    def test_missing_custom_and_default_uses_synthetic_black(self) -> None:
        selection = resolve_standby_asset(
            str(self.root / "missing-custom.png"),
            str(self.root / "missing-default.png"),
        )
        self.assertEqual(selection.source, "SYNTHETIC_BLACK")
        self.assertIsNone(selection.path)
        self.assertEqual(
            selection.fallback_reason, "ASSET_AND_NODE_DEFAULT_UNAVAILABLE"
        )
        self.assertIn("videotestsrc", gst_standby_source(selection))

    def test_public_status_never_exposes_local_path(self) -> None:
        custom = self.root / "secret-user-filename.png"
        write_png(custom, width=16, height=9)
        selection = resolve_standby_asset(str(custom), str(self.fallback))
        status = public_standby_status(selection)
        serialized = repr(status)
        self.assertNotIn(str(custom), serialized)
        self.assertNotIn(custom.name, serialized)
        self.assertEqual(status["source"], "CUSTOM")

    def test_png_generator_writes_real_png(self) -> None:
        generated = self.root / "generated.png"
        write_png(generated, width=8, height=4)
        payload = generated.read_bytes()
        self.assertTrue(payload.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertGreater(len(payload), 32)


if __name__ == "__main__":
    unittest.main()
