from __future__ import annotations

import argparse
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

import provider_state_reconcile_cli as reconcile  # noqa: E402


class ProviderStateReconcileCliTest(unittest.TestCase):
    def test_missing_fake_inventory_is_not_treated_as_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing-provider.json"
            args = argparse.Namespace(provider_mode="fake", fake_state_file=path)

            with self.assertRaises(ValueError):
                reconcile._provider_from_args(args)

            self.assertFalse(path.exists())

    def test_valid_fake_inventory_is_loaded_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "provider.json"
            payload = '{"volumes": [], "servers": [], "server_volume_links": {}}'
            path.write_text(payload, encoding="utf-8")
            before = path.stat()
            args = argparse.Namespace(provider_mode="fake", fake_state_file=path)

            provider = reconcile._provider_from_args(args)

            self.assertEqual(provider.list_managed_resources(), [])
            after = path.stat()
            self.assertEqual(
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns),
                (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns),
            )

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
                reconcile._provider_from_args(args)

    def test_fake_inventory_replacement_during_read_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "provider.json"
            payload = '{"volumes": [], "servers": [], "server_volume_links": {}}'
            path.write_text(payload, encoding="utf-8")
            args = argparse.Namespace(provider_mode="fake", fake_state_file=path)
            read_original = reconcile._read_json_authority_fd

            def read_then_replace(fd: int):
                value = read_original(fd)
                replacement = root / "provider-replacement.json"
                replacement.write_text(payload, encoding="utf-8")
                os.replace(replacement, path)
                return value

            with mock.patch.object(
                reconcile, "_read_json_authority_fd", side_effect=read_then_replace
            ):
                with self.assertRaises(ValueError):
                    reconcile._provider_from_args(args)


if __name__ == "__main__":
    unittest.main()
