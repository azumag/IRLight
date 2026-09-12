import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-network-egress-health.sh"
IPV4_HEADER = "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
ZERO6 = "0" * 32


def ipv4_route(interface="eth0", *, destination="00000000", mask="00000000", flags="0003"):
    return f"{interface}\t{destination}\t0100000A\t{flags}\t0\t0\t100\t{mask}\t0\t0\t0\n"


def ipv6_route(interface="eth0", *, flags="00000003"):
    return (
        f"{ZERO6} 00 {ZERO6} 00 fe800000000000000000000000000001 "
        f"00000400 00000000 00000000 {flags} {interface}\n"
    )


class NetworkEgressHealthTest(unittest.TestCase):
    def run_check(
        self,
        *,
        family,
        operstate="up",
        ipv4_content=None,
        ipv6_content=None,
        interface="eth0",
        env=None,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            interface_dir = root / "net" / interface
            interface_dir.mkdir(parents=True)
            (interface_dir / "operstate").write_text(f"{operstate}\n", encoding="ascii")

            ipv4_table = root / "route"
            ipv4_table.write_text(
                IPV4_HEADER + (ipv4_route(interface) if ipv4_content is None else ipv4_content),
                encoding="ascii",
            )
            ipv6_table = root / "ipv6_route"
            ipv6_table.write_text(
                ipv6_route(interface) if ipv6_content is None else ipv6_content,
                encoding="ascii",
            )

            merged_env = os.environ.copy()
            if env:
                merged_env.update(env)
            result = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    interface,
                    family,
                    str(interface_dir),
                    str(ipv4_table),
                    str(ipv6_table),
                ],
                text=True,
                capture_output=True,
                check=False,
                env=merged_env,
            )
            return result

    def test_ipv4_mode_requires_link_and_ipv4_default(self):
        result = self.run_check(family="ipv4")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_NETWORK_EGRESS_HEALTH status=OK link_status=OK "
            "ipv4_route_status=OK ipv6_route_status=NOT_REQUIRED family=ipv4",
        )

    def test_ipv6_mode_requires_link_and_ipv6_default(self):
        result = self.run_check(family="ipv6")
        self.assertEqual(result.returncode, 0)
        self.assertIn("status=OK", result.stdout)
        self.assertIn("ipv4_route_status=NOT_REQUIRED", result.stdout)
        self.assertIn("ipv6_route_status=OK", result.stdout)

    def test_dual_mode_requires_both_default_routes(self):
        result = self.run_check(family="dual")
        self.assertEqual(result.returncode, 0)
        self.assertIn("ipv4_route_status=OK", result.stdout)
        self.assertIn("ipv6_route_status=OK", result.stdout)

    def test_non_required_family_does_not_affect_result(self):
        result = self.run_check(family="ipv4", ipv6_content="broken row\n")
        self.assertEqual(result.returncode, 0)
        self.assertIn("ipv6_route_status=NOT_REQUIRED", result.stdout)

    def test_missing_required_route_is_critical(self):
        result = self.run_check(family="dual", ipv6_content="")
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("ipv6_route_status=CRITICAL", result.stdout)

    def test_malformed_required_route_is_unknown(self):
        result = self.run_check(family="dual", ipv6_content="broken row\n")
        self.assertEqual(result.returncode, 3)
        self.assertIn("status=UNKNOWN", result.stdout)
        self.assertIn("ipv6_route_status=UNKNOWN", result.stdout)

    def test_confirmed_link_failure_wins_over_route_unknown(self):
        result = self.run_check(family="dual", operstate="down", ipv6_content="broken row\n")
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("link_status=CRITICAL", result.stdout)
        self.assertIn("ipv6_route_status=UNKNOWN", result.stdout)

    def test_link_warning_propagates_when_routes_are_ok(self):
        result = self.run_check(family="dual", operstate="dormant")
        self.assertEqual(result.returncode, 1)
        self.assertIn("status=WARNING", result.stdout)
        self.assertIn("link_status=WARNING", result.stdout)

    def test_address_family_is_explicit_and_validated(self):
        for family in ("", "any", "IPv4"):
            with self.subTest(family=family):
                result = self.run_check(family=family)
                self.assertEqual(result.returncode, 3)
                self.assertIn("status=UNKNOWN", result.stdout)

    def test_invalid_interface_is_rejected_before_component_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                ["bash", str(SCRIPT), "bad/name", "ipv4", str(Path(tmp) / "missing")],
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 3)
        self.assertIn("reason=invalid_target", result.stdout)

    def test_environment_configuration_is_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            interface_dir = root / "net" / "ens3"
            interface_dir.mkdir(parents=True)
            (interface_dir / "operstate").write_text("up\n", encoding="ascii")
            ipv4_table = root / "route"
            ipv4_table.write_text(IPV4_HEADER + ipv4_route("ens3"), encoding="ascii")
            ipv6_table = root / "ipv6_route"
            ipv6_table.write_text(ipv6_route("ens3"), encoding="ascii")
            env = os.environ.copy()
            env.update(
                {
                    "IRLIGHT_NETWORK_INTERFACE": "ens3",
                    "IRLIGHT_NETWORK_ADDRESS_FAMILY": "dual",
                    "IRLIGHT_NETWORK_INTERFACE_DIR": str(interface_dir),
                    "IRLIGHT_IPV4_ROUTE_TABLE": str(ipv4_table),
                    "IRLIGHT_IPV6_ROUTE_TABLE": str(ipv6_table),
                }
            )
            result = subprocess.run(
                ["bash", str(SCRIPT)],
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )
        self.assertEqual(result.returncode, 0)
        self.assertIn("status=OK", result.stdout)
        self.assertIn("family=dual", result.stdout)


if __name__ == "__main__":
    unittest.main()
