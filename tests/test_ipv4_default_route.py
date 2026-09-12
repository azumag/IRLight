from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-ipv4-default-route.sh"
HEADER = "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"


class Ipv4DefaultRouteCheckTest(unittest.TestCase):
    def _run(self, body: str, interface: str = "eth0") -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="irlight-ipv4-route-") as temporary:
            route_table = Path(temporary) / "route"
            route_table.write_text(HEADER + body, encoding="utf-8")
            return subprocess.run(
                ["bash", str(SCRIPT), interface, str(route_table)],
                text=True,
                capture_output=True,
                check=False,
            )

    def test_up_non_reject_default_route_is_ok(self) -> None:
        result = self._run("eth0\t00000000\t010200C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_IPV4_DEFAULT_ROUTE status=OK route=default",
        )

    def test_default_route_on_other_interface_does_not_satisfy_target(self) -> None:
        result = self._run("eth1\t00000000\t010200C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_IPV4_DEFAULT_ROUTE status=CRITICAL reason=default_route_missing",
        )

    def test_non_default_route_does_not_satisfy_target(self) -> None:
        result = self._run("eth0\t0002A8C0\t00000000\t0001\t0\t0\t100\t00FFFFFF\t0\t0\t0\n")
        self.assertEqual(result.returncode, 2)
        self.assertIn("reason=default_route_missing", result.stdout)

    def test_down_or_reject_default_route_is_critical(self) -> None:
        for flags in ("0000", "0201", "0203"):
            with self.subTest(flags=flags):
                result = self._run(
                    f"eth0\t00000000\t010200C0\t{flags}\t0\t0\t100\t00000000\t0\t0\t0\n"
                )
                self.assertEqual(result.returncode, 2)
                self.assertEqual(
                    result.stdout.strip(),
                    "IRLIGHT_IPV4_DEFAULT_ROUTE status=CRITICAL reason=default_route_unusable",
                )

    def test_malformed_target_row_fails_closed_when_no_valid_default_exists(self) -> None:
        for row in (
            "eth0\t00000000\n",
            "eth0\tZZZZZZZZ\t010200C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n",
            "eth0\t00000000\t010200C0\tZZ\t0\t0\t100\t00000000\t0\t0\t0\n",
        ):
            with self.subTest(row=row):
                result = self._run(row)
                self.assertEqual(result.returncode, 3)
                self.assertEqual(
                    result.stdout.strip(),
                    "IRLIGHT_IPV4_DEFAULT_ROUTE status=UNKNOWN reason=invalid_target_route",
                )

    def test_unrelated_malformed_row_does_not_hide_valid_target_default(self) -> None:
        result = self._run(
            "eth1\tbroken\n"
            "eth0\t00000000\t010200C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
        )
        self.assertEqual(result.returncode, 0)

    def test_valid_target_default_wins_over_other_malformed_target_row(self) -> None:
        result = self._run(
            "eth0\tbroken\n"
            "eth0\t00000000\t010200C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
        )
        self.assertEqual(result.returncode, 0)

    def test_invalid_header_or_empty_table_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-ipv4-route-invalid-") as temporary:
            for content in ("", "Iface Destination\n"):
                route_table = Path(temporary) / "route"
                route_table.write_text(content, encoding="utf-8")
                result = subprocess.run(
                    ["bash", str(SCRIPT), "eth0", str(route_table)],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 3)
                self.assertEqual(
                    result.stdout.strip(),
                    "IRLIGHT_IPV4_DEFAULT_ROUTE status=UNKNOWN reason=invalid_route_table",
                )

    def test_target_is_required_and_invalid_target_is_rejected(self) -> None:
        env = {
            key: value
            for key, value in os.environ.items()
            if key != "IRLIGHT_NETWORK_INTERFACE"
        }
        result = subprocess.run(
            ["bash", str(SCRIPT)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_IPV4_DEFAULT_ROUTE status=UNKNOWN reason=target_required",
        )

        result = self._run("", interface="bad interface")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_IPV4_DEFAULT_ROUTE status=UNKNOWN reason=invalid_target",
        )

    def test_environment_overrides_are_supported(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-ipv4-route-env-") as temporary:
            route_table = Path(temporary) / "route"
            route_table.write_text(
                HEADER
                + "eth0\t00000000\t010200C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["IRLIGHT_NETWORK_INTERFACE"] = "eth0"
            env["IRLIGHT_IPV4_ROUTE_TABLE"] = str(route_table)
            result = subprocess.run(
                ["bash", str(SCRIPT)],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
