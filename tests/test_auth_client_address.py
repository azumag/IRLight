from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.requests import Request as HttpRequest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from auth_client_address import (  # noqa: E402
    DEFAULT_MAX_FORWARDED_HOPS,
    MAX_FORWARDED_HEADER_BYTES,
    client_address,
    trusted_proxies_from_env,
)


def _request(
    address: str | None = "192.0.2.10",
    forwarded_for: list[str] | None = None,
) -> HttpRequest:
    headers = [
        (b"x-forwarded-for", value.encode("ascii")) for value in (forwarded_for or [])
    ]
    scope: dict[str, object] = {"type": "http", "headers": headers}
    if address is not None:
        scope["client"] = (address, 12345)
    return HttpRequest(scope)


class TrustedProxyEnvironmentTest(unittest.TestCase):
    def test_unset_or_invalid_entries_are_ignored(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("IRLIGHT_TRUSTED_PROXIES", None)
            self.assertEqual(trusted_proxies_from_env(), frozenset())
        with patch.dict(
            os.environ,
            {"IRLIGHT_TRUSTED_PROXIES": " ,not-an-ip,10.0.0.0/8,192.0.2.1, "},
            clear=False,
        ):
            self.assertEqual(trusted_proxies_from_env(), frozenset({"192.0.2.1"}))

    def test_addresses_are_canonicalized(self) -> None:
        with patch.dict(
            os.environ,
            {"IRLIGHT_TRUSTED_PROXIES": "2001:0db8:0000:0000:0000:0000:0000:0001"},
            clear=False,
        ):
            self.assertEqual(trusted_proxies_from_env(), frozenset({"2001:db8::1"}))


class ClientAddressTest(unittest.TestCase):
    def test_forwarded_header_is_ignored_without_a_trusted_proxy(self) -> None:
        request = _request("192.0.2.10", ["203.0.113.7"])
        self.assertEqual(client_address(request), "192.0.2.10")
        self.assertEqual(client_address(request, trusted_proxies=[]), "192.0.2.10")

    def test_untrusted_peer_cannot_spoof_the_client_address(self) -> None:
        request = _request("198.51.100.4", ["203.0.113.7, 192.0.2.10"])
        self.assertEqual(client_address(request, trusted_proxies=["192.0.2.10"]), "198.51.100.4")

    def test_trusted_peer_uses_the_first_untrusted_hop(self) -> None:
        request = _request("192.0.2.10", ["203.0.113.7, 192.0.2.20"])
        self.assertEqual(
            client_address(request, trusted_proxies=["192.0.2.10", "192.0.2.20"]),
            "203.0.113.7",
        )

    def test_single_hop_behind_a_trusted_proxy(self) -> None:
        request = _request("192.0.2.10", ["203.0.113.7"])
        self.assertEqual(client_address(request, trusted_proxies=["192.0.2.10"]), "203.0.113.7")

    def test_trusted_peer_without_the_header_uses_the_peer(self) -> None:
        request = _request("192.0.2.10")
        self.assertEqual(client_address(request, trusted_proxies=["192.0.2.10"]), "192.0.2.10")

    def test_malformed_hop_falls_back_to_the_peer(self) -> None:
        for value in ("unknown", "203.0.113.7, not-an-ip", "203.0.113.7,", "  "):
            with self.subTest(value=value):
                request = _request("192.0.2.10", [value])
                self.assertEqual(
                    client_address(request, trusted_proxies=["192.0.2.10"]), "192.0.2.10"
                )

    def test_oversized_or_repeated_headers_are_not_trusted(self) -> None:
        oversized = _request("192.0.2.10", ["1" * (MAX_FORWARDED_HEADER_BYTES + 1)])
        self.assertEqual(client_address(oversized, trusted_proxies=["192.0.2.10"]), "192.0.2.10")

        repeated = _request("192.0.2.10", ["203.0.113.7", "203.0.113.8"])
        self.assertEqual(client_address(repeated, trusted_proxies=["192.0.2.10"]), "192.0.2.10")

    def test_too_many_hops_are_not_trusted(self) -> None:
        hops = ", ".join(f"203.0.113.{index}" for index in range(DEFAULT_MAX_FORWARDED_HOPS + 1))
        request = _request("192.0.2.10", [hops])
        self.assertEqual(client_address(request, trusted_proxies=["192.0.2.10"]), "192.0.2.10")

    def test_all_trusted_hops_fall_back_to_the_peer(self) -> None:
        request = _request("192.0.2.10", ["192.0.2.20, 192.0.2.30"])
        self.assertEqual(
            client_address(request, trusted_proxies=["192.0.2.10", "192.0.2.20", "192.0.2.30"]),
            "192.0.2.10",
        )

    def test_ipv6_matching_is_canonicalized(self) -> None:
        request = _request("::ffff:192.0.2.10", ["2001:0db8::1"])
        self.assertEqual(
            client_address(request, trusted_proxies=["::ffff:192.0.2.10"]),
            "2001:db8::1",
        )

    def test_missing_peer_address_is_reported_as_unknown(self) -> None:
        self.assertIsNone(client_address(_request(address=None)))
        self.assertIsNone(client_address(object()))
        self.assertIsNone(client_address(_request("not-an-ip")))


if __name__ == "__main__":
    unittest.main()
