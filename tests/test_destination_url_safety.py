from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from destination_url_safety import (  # noqa: E402
    DestinationUrlSafetyError,
    validate_destination_url_secret_safety,
)


class DestinationUrlSafetyTest(unittest.TestCase):
    def test_allows_compact_routing_only_streamid(self) -> None:
        validate_destination_url_secret_safety(
            "srt://stream.example:8890?STREAMID=publish:live/input&latency=120&mode=CALLER"
        )

    def test_allows_structured_routing_only_streamid(self) -> None:
        validate_destination_url_secret_safety(
            "srt://stream.example:8890?streamid=%23!%3A%3Ar=live%2Finput%2Cm=publish"
        )

    def test_rejects_case_insensitive_passphrase_without_echoing_secret(self) -> None:
        secret = "AUDIT_DUMMY_SECRET"
        with self.assertRaises(DestinationUrlSafetyError) as raised:
            validate_destination_url_secret_safety(
                f"srt://stream.example:8890?PaSsPhRaSe={secret}"
            )
        self.assertNotIn(secret, str(raised.exception))

    def test_rejects_encoded_value_injection_without_echoing_secret(self) -> None:
        secret = "AUDIT_DUMMY_SECRET"
        with self.assertRaisesRegex(DestinationUrlSafetyError, "decimal integer") as raised:
            validate_destination_url_secret_safety(
                "srt://stream.example:8890?latency="
                f"120%2526passphrase%253D{secret}"
            )
        self.assertNotIn(secret, str(raised.exception))

    def test_rejects_unknown_query_without_echoing_value(self) -> None:
        secret = "AUDIT_DUMMY_SECRET"
        with self.assertRaisesRegex(DestinationUrlSafetyError, "unsupported query") as raised:
            validate_destination_url_secret_safety(
                f"srt://stream.example:8890?futuresecret={secret}"
            )
        self.assertNotIn(secret, str(raised.exception))

    def test_rejects_opaque_streamid_outside_public_routing_syntax(self) -> None:
        with self.assertRaisesRegex(DestinationUrlSafetyError, "authenticated SRT streamid"):
            validate_destination_url_secret_safety(
                "srt://stream.example:8890?streamid=opaque-arbitrary-token"
            )

    def test_rejects_srt_path_as_opaque_channel(self) -> None:
        with self.assertRaisesRegex(DestinationUrlSafetyError, "path is not supported"):
            validate_destination_url_secret_safety(
                "srt://stream.example:8890/AUDIT_DUMMY_SECRET?streamid=publish:probe"
            )

    def test_rejects_srt_fragment_as_opaque_channel(self) -> None:
        with self.assertRaisesRegex(DestinationUrlSafetyError, "fragments are not supported"):
            validate_destination_url_secret_safety(
                "srt://stream.example:8890?streamid=publish:probe#AUDIT_DUMMY_SECRET"
            )


if __name__ == "__main__":
    unittest.main()
