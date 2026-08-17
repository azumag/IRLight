from __future__ import annotations

import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

# Point the store at a throwaway STATE_DIR before importing.
_TMP = tempfile.mkdtemp(prefix="irlight-catalog-")
os.environ["STATE_DIR"] = _TMP

from catalog_store import (  # noqa: E402
    CATALOG_PATH,
    CatalogNotFound,
    create_asset,
    create_destination,
    delete_asset,
    delete_destination,
    ensure_catalog,
    get_asset,
    get_destination,
    list_assets,
    list_destinations,
    update_destination,
)


class CatalogStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        if CATALOG_PATH.exists():
            CATALOG_PATH.unlink()
        ensure_catalog()

    def _create_destination(self, user_id: str = "deadbeef") -> dict[str, object]:
        return create_destination(
            user_id=user_id,
            type="rtmp",
            display_name="Twitch",
            server_url="rtmp://live.twitch.tv/app",
            secret_ref="secret/twitch-1",
        )

    def test_create_and_get_destination(self) -> None:
        item = self._create_destination()
        fetched = get_destination(str(item["id"]), "deadbeef")
        self.assertEqual(fetched["display_name"], "Twitch")
        self.assertEqual(fetched["verification_status"], "UNVERIFIED")

    def test_list_only_returns_owned(self) -> None:
        self._create_destination(user_id="alice")
        self._create_destination(user_id="bob")
        self.assertEqual(len(list_destinations("alice")), 1)
        self.assertEqual(len(list_destinations("bob")), 1)

    def test_cross_user_access_rejected(self) -> None:
        item = self._create_destination(user_id="alice")
        with self.assertRaises(CatalogNotFound):
            get_destination(str(item["id"]), "bob")

    def test_update_destination(self) -> None:
        item = self._create_destination()
        updated = update_destination(
            str(item["id"]), user_id="deadbeef", display_name="YouTube"
        )
        self.assertEqual(updated["display_name"], "YouTube")

    def test_delete_destination(self) -> None:
        item = self._create_destination()
        delete_destination(str(item["id"]), "deadbeef")
        with self.assertRaises(CatalogNotFound):
            get_destination(str(item["id"]), "deadbeef")

    def test_asset_lifecycle(self) -> None:
        asset = create_asset(user_id="deadbeef", source_object_key="uploads/standby.png")
        self.assertEqual(asset["processing_status"], "PENDING")
        fetched = get_asset(str(asset["id"]), "deadbeef")
        self.assertEqual(fetched["source_object_key"], "uploads/standby.png")
        self.assertEqual(len(list_assets("deadbeef")), 1)
        delete_asset(str(asset["id"]), "deadbeef")
        self.assertEqual(len(list_assets("deadbeef")), 0)


if __name__ == "__main__":
    unittest.main()