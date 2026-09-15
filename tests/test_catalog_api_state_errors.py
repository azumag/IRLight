from __future__ import annotations

import sys
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

import catalog_api  # noqa: E402
from catalog_store import CatalogStateError  # noqa: E402


class CatalogApiStateErrorTest(unittest.TestCase):
    def assert_state_unavailable(self, callback) -> None:
        with self.assertRaises(HTTPException) as raised:
            callback()
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(
            raised.exception.detail,
            {"code": catalog_api.CATALOG_STATE_UNAVAILABLE_CODE},
        )
        self.assertNotIn("/state/", str(raised.exception.detail))
        self.assertNotIn("catalog.json", str(raised.exception.detail))

    @staticmethod
    def _state_error() -> CatalogStateError:
        return CatalogStateError("catalog state /state/catalog.json cannot be read")

    def test_destination_create_maps_state_error(self) -> None:
        request = catalog_api.DestinationCreate(
            type="rtmp",
            display_name="Twitch",
            server_url="rtmp://live.example.test/app",
            secret_ref="secret/twitch",
        )
        with patch(
            "catalog_api.store_create_destination", side_effect=self._state_error()
        ):
            self.assert_state_unavailable(
                lambda: catalog_api.create_destination(
                    request, {"id": "user-a"}, _csrf=None
                )
            )

    def test_destination_read_paths_map_state_error(self) -> None:
        cases = (
            (
                "catalog_api.store_list_destinations",
                lambda: catalog_api.list_destinations({"id": "user-a"}),
            ),
            (
                "catalog_api.store_get_destination",
                lambda: catalog_api.get_destination("destination-a", {"id": "user-a"}),
            ),
        )
        for target, callback in cases:
            with self.subTest(target=target):
                with patch(target, side_effect=self._state_error()):
                    self.assert_state_unavailable(callback)

    def test_destination_mutation_paths_map_state_error(self) -> None:
        request = catalog_api.DestinationUpdate(display_name="Updated")
        with patch(
            "catalog_api.store_update_destination", side_effect=self._state_error()
        ):
            self.assert_state_unavailable(
                lambda: catalog_api.update_destination(
                    "destination-a", request, {"id": "user-a"}, _csrf=None
                )
            )

        with patch(
            "catalog_api.store_delete_destination", side_effect=self._state_error()
        ):
            self.assert_state_unavailable(
                lambda: catalog_api.delete_destination(
                    "destination-a", {"id": "user-a"}, _csrf=None
                )
            )

    def test_destination_secret_routes_map_catalog_lookup_state_error(self) -> None:
        secret_request = catalog_api.DestinationSecretUpdate(value="dummy-secret")
        with patch(
            "catalog_api.store_get_destination", side_effect=self._state_error()
        ):
            self.assert_state_unavailable(
                lambda: catalog_api.put_destination_secret(
                    "destination-a",
                    secret_request,
                    {"id": "user-a"},
                    _csrf=None,
                )
            )

        with patch(
            "catalog_api.store_get_destination", side_effect=self._state_error()
        ):
            self.assert_state_unavailable(
                lambda: catalog_api.delete_destination_secret(
                    "destination-a", {"id": "user-a"}, _csrf=None
                )
            )

    def test_destination_verify_maps_state_error(self) -> None:
        with (
            patch("catalog_api.destination_probe_slot", return_value=nullcontext()),
            patch(
                "catalog_api.store_verify_destination", side_effect=self._state_error()
            ),
        ):
            self.assert_state_unavailable(
                lambda: catalog_api.verify_destination(
                    "destination-a", {"id": "user-a"}, _csrf=None
                )
            )

    def test_asset_paths_map_state_error(self) -> None:
        asset_request = catalog_api.AssetCreate(source_object_key="uploads/standby.png")
        cases = (
            (
                "catalog_api.store_create_asset",
                lambda: catalog_api.create_asset(
                    asset_request, {"id": "user-a"}, _csrf=None
                ),
            ),
            (
                "catalog_api.store_list_assets",
                lambda: catalog_api.list_assets({"id": "user-a"}),
            ),
            (
                "catalog_api.store_get_asset",
                lambda: catalog_api.get_asset("asset-a", {"id": "user-a"}),
            ),
            (
                "catalog_api.store_delete_asset",
                lambda: catalog_api.delete_asset(
                    "asset-a", {"id": "user-a"}, _csrf=None
                ),
            ),
        )
        for target, callback in cases:
            with self.subTest(target=target):
                with patch(target, side_effect=self._state_error()):
                    self.assert_state_unavailable(callback)


if __name__ == "__main__":
    unittest.main()
