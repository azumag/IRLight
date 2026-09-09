from __future__ import annotations

import argparse
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from provider_state_reconcile_cli import _provider_from_args  # noqa: E402


class ProviderStateReconcileCliTest(unittest.TestCase):
    def test_missing_fake_inventory_is_not_treated_as_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing-provider.json"
            args = argparse.Namespace(provider_mode="fake", fake_state_file=path)

            with self.assertRaises(ValueError):
                _provider_from_args(args)

            self.assertFalse(path.exists())

    def test_fake_inventory_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "provider.json"
            target.write_text(
                '{"volumes": [], "servers": [], "server_volume_links": {}}',
                encoding="utf-8",
            )
            link = root / "provider-link.json"
            link.symlink_to(target)
            args = argparse.Namespace(provider_mode="fake", fake_state_file=link)

            with self.assertRaises(ValueError):
                _provider_from_args(args)


if __name__ == "__main__":
    unittest.main()
