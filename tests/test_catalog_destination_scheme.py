from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

# Keep this module isolated even when it is run directly instead of through the
# full unittest discovery order.
os.environ.setdefault("STATE_DIR", tempfile.mkdtemp(prefix="irlight-catalog-scheme-"))

from catalog_store import (  # noqa: E402
    CATALOG_PATH,
    CatalogVerifyFailed,
    create_destination,
    ensure_catalog,
    get_destination,
    verify_destination,
)


class CatalogDestinationSchemeTest(unittest.TestCase):
    def setUp(self) -> None:
        CATALOG_PATH.unlink(missing_ok=True)
        ensure_catalog()

    def test_verification_rejects_type_url_scheme_mismatch_before_probe(self) -> None:
        destination = create_destination(
            user_id="user-scheme",
            type="rtmp",
            display_name="Mismatch",
            server_url="rtmps://stream.example/app",
            secret_ref="secret/mismatch",
        )
        called = False

        def probe(_url: str):
            nonlocal called
            called = True
            return {
                "protocol": "rtmps",
                "peer_ip": "8.8.8.8",
                "peer_port": 443,
                "elapsed_ms": 1.0,
            }

        with self.assertRaisesRegex(CatalogVerifyFailed, "does not match"):
            verify_destination(
                str(destination["id"]),
                "user-scheme",
                probe=probe,
            )
        self.assertFalse(called)
        fetched = get_destination(str(destination["id"]), "user-scheme")
        self.assertEqual(fetched["verification_status"], "FAILED")
        self.assertIn("does not match", fetched["last_verification_error"])

    def test_probe_protocol_must_match_destination_type(self) -> None:
        destination = create_destination(
            user_id="user-protocol",
            type="rtmp",
            display_name="Protocol mismatch",
            server_url="rtmp://stream.example/app",
            secret_ref="secret/protocol",
        )
        with self.assertRaisesRegex(CatalogVerifyFailed, "probe protocol"):
            verify_destination(
                str(destination["id"]),
                "user-protocol",
                probe=lambda _url: {
                    "protocol": "rtmps",
                    "peer_ip": "8.8.8.8",
                    "peer_port": 1935,
                    "elapsed_ms": 1.0,
                },
            )
        fetched = get_destination(str(destination["id"]), "user-protocol")
        self.assertEqual(fetched["verification_status"], "FAILED")


if __name__ == "__main__":
    unittest.main()
