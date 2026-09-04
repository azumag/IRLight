from __future__ import annotations

import os
import multiprocessing
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
    CatalogStateError,
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
from state_safety import initialization_marker  # noqa: E402


def _create_assets_in_child(prefix: str, count: int) -> None:
    for index in range(count):
        create_asset(
            user_id="parallel-user",
            source_object_key=f"{prefix}/{index}.png",
        )


class CatalogStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        if CATALOG_PATH.exists():
            CATALOG_PATH.unlink()
        initialization_marker(CATALOG_PATH).unlink(missing_ok=True)
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

    def test_initialized_catalog_deletion_fails_closed(self) -> None:
        self._create_destination()
        CATALOG_PATH.unlink()
        with self.assertRaises(CatalogStateError):
            self._create_destination(user_id="other")

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

    def test_processes_do_not_clobber_catalog_updates(self) -> None:
        context = multiprocessing.get_context("fork")
        processes = [
            context.Process(target=_create_assets_in_child, args=(f"worker-{index}", 10))
            for index in range(2)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=10)
            self.assertEqual(process.exitcode, 0)

        self.assertEqual(len(list_assets("parallel-user")), 20)

    def test_corrupt_catalog_fails_closed(self) -> None:
        CATALOG_PATH.write_text("[]", encoding="utf-8")
        with self.assertRaises(CatalogStateError):
            list_assets("deadbeef")


if __name__ == "__main__":
    unittest.main()
