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

# Point the store at a throwaway STATE_DIR before importing.
_TMP = tempfile.mkdtemp(prefix="irlight-catalog-")
os.environ["STATE_DIR"] = _TMP

from catalog_store import (  # noqa: E402
    CATALOG_PATH,
    CatalogNotFound,
    CatalogValidationError,
    CatalogVerifyFailed,
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
    verify_destination,
)
from destination_probe import DestinationProbeError  # noqa: E402


class CatalogStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        if CATALOG_PATH.exists():
            CATALOG_PATH.unlink()
        ensure_catalog()

    def _create_destination(
        self, user_id: str = "deadbeef", platform: str = "custom"
    ) -> dict[str, object]:
        return create_destination(
            user_id=user_id,
            platform=platform,
            type="rtmp",
            display_name="Twitch",
            server_url="rtmp://live.twitch.tv/app",
            secret_ref="secret/twitch-1",
        )

    def test_create_and_get_destination(self) -> None:
        item = self._create_destination()
        fetched = get_destination(str(item["id"]), "deadbeef")
        self.assertEqual(fetched["display_name"], "Twitch")
        self.assertEqual(fetched["platform"], "custom")
        self.assertEqual(fetched["verification_status"], "UNVERIFIED")

    def test_four_mvp_platforms_can_be_registered(self) -> None:
        for platform in ("twitch", "youtube", "kick", "custom"):
            with self.subTest(platform=platform):
                item = self._create_destination(platform=platform)
                self.assertEqual(item["platform"], platform)
                self.assertEqual(item["type"], "rtmp")

    def test_custom_can_use_srt_but_managed_platforms_cannot(self) -> None:
        custom = create_destination(
            user_id="deadbeef",
            platform="custom",
            type="srt",
            display_name="Custom SRT",
            server_url="srt://stream.example:9000?mode=caller",
            secret_ref="secret/custom-srt",
        )
        self.assertEqual(custom["platform"], "custom")
        with self.assertRaisesRegex(CatalogValidationError, "does not support"):
            create_destination(
                user_id="deadbeef",
                platform="twitch",
                type="srt",
                display_name="Invalid Twitch SRT",
                server_url="srt://stream.example:9000?mode=caller",
                secret_ref="secret/twitch-srt",
            )

    def test_legacy_destination_without_platform_reads_as_custom(self) -> None:
        item = self._create_destination()
        payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        del payload["destinations"][str(item["id"])]["platform"]
        CATALOG_PATH.write_text(json.dumps(payload), encoding="utf-8")

        fetched = get_destination(str(item["id"]), "deadbeef")
        listed = list_destinations("deadbeef")
        self.assertEqual(fetched["platform"], "custom")
        self.assertEqual(listed[0]["platform"], "custom")

    def test_platform_update_validates_existing_protocol(self) -> None:
        item = self._create_destination(platform="custom")
        updated = update_destination(
            str(item["id"]), user_id="deadbeef", platform="youtube"
        )
        self.assertEqual(updated["platform"], "youtube")

        srt = create_destination(
            user_id="deadbeef",
            platform="custom",
            type="srt",
            display_name="Custom SRT",
            server_url="srt://stream.example:9000?mode=caller",
            secret_ref="secret/custom-srt-2",
        )
        with self.assertRaisesRegex(CatalogValidationError, "does not support"):
            update_destination(
                str(srt["id"]), user_id="deadbeef", platform="kick"
            )

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

    def test_server_url_change_resets_verification(self) -> None:
        item = self._create_destination()
        verified = verify_destination(
            str(item["id"]),
            "deadbeef",
            probe=lambda _url: {
                "protocol": "rtmp",
                "peer_ip": "203.0.113.10",
                "peer_port": 1935,
                "elapsed_ms": 1.0,
            },
        )
        self.assertEqual(verified["verification_status"], "VERIFIED")
        updated = update_destination(
            str(item["id"]),
            user_id="deadbeef",
            server_url="rtmp://example.com/live",
        )
        self.assertEqual(updated["verification_status"], "UNVERIFIED")
        self.assertIsNone(updated["last_verified_at"])
        self.assertIsNone(updated["verification_transport"])

    def test_delete_destination(self) -> None:
        item = self._create_destination()
        delete_destination(str(item["id"]), "deadbeef")
        with self.assertRaises(CatalogNotFound):
            get_destination(str(item["id"]), "deadbeef")

    def test_verify_destination_success(self) -> None:
        item = self._create_destination()
        verified = verify_destination(
            str(item["id"]),
            "deadbeef",
            probe=lambda _url: {
                "protocol": "rtmp",
                "peer_ip": "203.0.113.10",
                "peer_port": 1935,
                "elapsed_ms": 12.5,
            },
        )
        self.assertEqual(verified["verification_status"], "VERIFIED")
        self.assertIsNotNone(verified["last_verified_at"])
        self.assertEqual(verified["verification_transport"]["protocol"], "rtmp")
        self.assertEqual(verified["verification_transport"]["peer_port"], 1935)

    def test_verify_destination_records_probe_failure(self) -> None:
        item = self._create_destination()

        def fail(_url: str) -> dict[str, object]:
            raise DestinationProbeError("destination did not complete the RTMP handshake")

        with self.assertRaisesRegex(CatalogVerifyFailed, "RTMP handshake"):
            verify_destination(str(item["id"]), "deadbeef", probe=fail)
        fetched = get_destination(str(item["id"]), "deadbeef")
        self.assertEqual(fetched["verification_status"], "FAILED")
        self.assertIn("RTMP handshake", fetched["last_verification_error"])
        self.assertIsNone(fetched["verification_transport"])

    def test_verify_destination_rejects_bad_scheme(self) -> None:
        item = create_destination(
            user_id="deadbeef",
            type="rtmp",
            display_name="Bad",
            server_url="https://example.com/live",
            secret_ref="secret/bad",
        )
        with self.assertRaises(CatalogVerifyFailed):
            verify_destination(str(item["id"]), "deadbeef")
        fetched = get_destination(str(item["id"]), "deadbeef")
        self.assertEqual(fetched["verification_status"], "FAILED")

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
