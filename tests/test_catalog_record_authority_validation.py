from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

_TMP = tempfile.mkdtemp(prefix="irlight-catalog-record-validation-")
os.environ["STATE_DIR"] = _TMP

from catalog_store import (  # noqa: E402
    CATALOG_PATH,
    CatalogStateError,
    create_asset,
    create_destination,
    ensure_catalog,
    list_assets,
    list_destinations,
)
from state_safety import initialization_marker  # noqa: E402


class CatalogRecordAuthorityValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        CATALOG_PATH.unlink(missing_ok=True)
        initialization_marker(CATALOG_PATH).unlink(missing_ok=True)
        ensure_catalog()

    def _write_catalog(self, *, destinations=None, assets=None) -> None:
        CATALOG_PATH.write_text(
            json.dumps(
                {
                    "destinations": destinations or {},
                    "assets": assets or {},
                }
            ),
            encoding="utf-8",
        )

    def _destination(self, destination_id: str) -> dict[str, object]:
        return {
            "id": destination_id,
            "user_id": "user-1",
            "type": "rtmp",
            "display_name": "Example",
            "server_url": "rtmp://example.com/live",
            "secret_ref": "secret/example",
        }

    def test_writer_generated_records_remain_readable(self) -> None:
        create_destination(
            user_id="user-1",
            type="rtmp",
            display_name="Example",
            server_url="rtmp://example.com/live",
            secret_ref="secret/example",
        )
        create_asset(user_id="user-1", source_object_key="uploads/standby.png")

        self.assertEqual(len(list_destinations("user-1")), 1)
        self.assertEqual(len(list_assets("user-1")), 1)

    def test_invalid_writer_input_does_not_replace_catalog(self) -> None:
        before = CATALOG_PATH.read_bytes()

        with self.assertRaises(CatalogStateError):
            create_destination(
                user_id="user-1",
                type="rtmp",
                display_name="",
                server_url="rtmp://example.com/live",
                secret_ref="secret/example",
            )
        self.assertEqual(CATALOG_PATH.read_bytes(), before)

        with self.assertRaises(CatalogStateError):
            create_asset(user_id="", source_object_key="uploads/standby.png")
        self.assertEqual(CATALOG_PATH.read_bytes(), before)

    def test_destination_key_and_record_id_must_match(self) -> None:
        destination_id = str(uuid.uuid4())
        record = self._destination(str(uuid.uuid4()))
        self._write_catalog(destinations={destination_id: record})

        before = CATALOG_PATH.read_bytes()
        with self.assertRaisesRegex(CatalogStateError, "id does not match its key"):
            list_destinations("user-1")
        self.assertEqual(CATALOG_PATH.read_bytes(), before)

    def test_destination_required_identity_and_secret_boundary_fields_fail_closed(self) -> None:
        destination_id = str(uuid.uuid4())
        baseline = self._destination(destination_id)
        invalid_values = {
            "user_id": None,
            "type": 1,
            "display_name": "",
            "server_url": {"url": "rtmp://example.com/live"},
            "secret_ref": ["secret/example"],
        }

        for field, invalid in invalid_values.items():
            with self.subTest(field=field):
                record = dict(baseline)
                record[field] = invalid
                self._write_catalog(destinations={destination_id: record})
                before = CATALOG_PATH.read_bytes()

                with self.assertRaises(CatalogStateError):
                    list_destinations("user-1")
                self.assertEqual(CATALOG_PATH.read_bytes(), before)

    def test_asset_identity_and_source_object_key_fail_closed(self) -> None:
        asset_id = str(uuid.uuid4())
        invalid_records = (
            {
                "id": str(uuid.uuid4()),
                "user_id": "user-1",
                "source_object_key": "uploads/standby.png",
            },
            {
                "id": asset_id,
                "user_id": None,
                "source_object_key": "uploads/standby.png",
            },
            {
                "id": asset_id,
                "user_id": "user-1",
                "source_object_key": 123,
            },
        )

        for record in invalid_records:
            with self.subTest(record=record):
                self._write_catalog(assets={asset_id: record})
                before = CATALOG_PATH.read_bytes()

                with self.assertRaises(CatalogStateError):
                    list_assets("user-1")
                self.assertEqual(CATALOG_PATH.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
