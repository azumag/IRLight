from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTROL_API = ROOT / "apps" / "control-api"


class CatalogOperationalMetadataValidationTest(unittest.TestCase):
    def _run_isolated(self, body: str) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-catalog-operational-") as state_dir:
            env = os.environ.copy()
            env["STATE_DIR"] = state_dir
            env["PYTHONPATH"] = str(CONTROL_API)
            result = subprocess.run(
                [sys.executable, "-c", textwrap.dedent(body)],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )

    def test_destination_operational_metadata_fails_closed(self) -> None:
        self._run_isolated(
            r'''
            import copy
            import json

            from catalog_store import (
                CATALOG_PATH,
                CatalogStateError,
                create_destination,
                ensure_catalog,
                list_destinations,
            )

            ensure_catalog()
            destination = create_destination(
                user_id="owner",
                type="rtmp",
                display_name="Destination",
                server_url="rtmp://example.com/live",
                secret_ref="secret/destination",
            )
            destination_id = destination["id"]
            baseline = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))

            invalid_values = {
                "enabled": "true",
                "verification_status": "",
                "last_verified_at": "123.0",
                "last_verification_error": ["unexpected"],
                "verification_transport": [],
            }
            for field, invalid_value in invalid_values.items():
                damaged = copy.deepcopy(baseline)
                damaged["destinations"][destination_id][field] = invalid_value
                CATALOG_PATH.write_text(json.dumps(damaged), encoding="utf-8")
                try:
                    list_destinations("owner")
                except CatalogStateError as exc:
                    assert field in str(exc), (field, exc)
                else:
                    raise AssertionError(f"invalid {field} was accepted")

            for field in ("verification_status", "last_verified_at"):
                damaged = copy.deepcopy(baseline)
                damaged["destinations"][destination_id].pop(field)
                CATALOG_PATH.write_text(json.dumps(damaged), encoding="utf-8")
                try:
                    list_destinations("owner")
                except CatalogStateError as exc:
                    assert field in str(exc), (field, exc)
                else:
                    raise AssertionError(f"missing {field} was accepted")
            '''
        )

    def test_historical_destination_shape_remains_readable(self) -> None:
        self._run_isolated(
            r'''
            import json

            from catalog_store import CATALOG_PATH, ensure_catalog, list_destinations

            ensure_catalog()
            historical = {
                "destinations": {
                    "destination-1": {
                        "id": "destination-1",
                        "user_id": "owner",
                        "type": "rtmp",
                        "display_name": "Historical destination",
                        "server_url": "rtmp://example.com/live",
                        "secret_ref": "secret/destination",
                        "verification_status": "UNVERIFIED",
                        "last_verified_at": None,
                        "created_at": 1.0,
                        "updated_at": 1.0,
                    }
                },
                "assets": {},
            }
            CATALOG_PATH.write_text(json.dumps(historical), encoding="utf-8")

            items = list_destinations("owner")
            assert len(items) == 1, items
            assert items[0]["id"] == "destination-1", items
            assert "enabled" not in items[0], items
            assert "last_verification_error" not in items[0], items
            assert "verification_transport" not in items[0], items
            '''
        )

    def test_asset_processing_status_fails_closed(self) -> None:
        self._run_isolated(
            r'''
            import copy
            import json

            from catalog_store import (
                CATALOG_PATH,
                CatalogStateError,
                create_asset,
                ensure_catalog,
                list_assets,
            )

            ensure_catalog()
            asset = create_asset(user_id="owner", source_object_key="uploads/standby.png")
            asset_id = asset["id"]
            baseline = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))

            for invalid_value in (None, False, 1, ""):
                damaged = copy.deepcopy(baseline)
                damaged["assets"][asset_id]["processing_status"] = invalid_value
                CATALOG_PATH.write_text(json.dumps(damaged), encoding="utf-8")
                try:
                    list_assets("owner")
                except CatalogStateError as exc:
                    assert "processing_status" in str(exc), exc
                else:
                    raise AssertionError(
                        f"invalid processing_status was accepted: {invalid_value!r}"
                    )

            damaged = copy.deepcopy(baseline)
            damaged["assets"][asset_id].pop("processing_status")
            CATALOG_PATH.write_text(json.dumps(damaged), encoding="utf-8")
            try:
                list_assets("owner")
            except CatalogStateError as exc:
                assert "processing_status" in str(exc), exc
            else:
                raise AssertionError("missing processing_status was accepted")
            '''
        )

    def test_invalid_writer_metadata_does_not_replace_catalog(self) -> None:
        self._run_isolated(
            r'''
            from catalog_store import (
                CATALOG_PATH,
                CatalogStateError,
                create_destination,
                ensure_catalog,
                get_destination,
                update_destination,
            )

            ensure_catalog()
            destination = create_destination(
                user_id="owner",
                type="rtmp",
                display_name="Destination",
                server_url="rtmp://example.com/live",
                secret_ref="secret/destination",
            )
            before = CATALOG_PATH.read_bytes()

            try:
                update_destination(
                    destination["id"],
                    user_id="owner",
                    enabled="yes",  # type: ignore[arg-type]
                )
            except CatalogStateError as exc:
                assert "enabled" in str(exc), exc
            else:
                raise AssertionError("invalid enabled writer value was accepted")

            assert CATALOG_PATH.read_bytes() == before
            fetched = get_destination(destination["id"], "owner")
            assert fetched["enabled"] is True
            '''
        )


if __name__ == "__main__":
    unittest.main()
