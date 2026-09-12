import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-ipv6-default-route.sh"
ZERO = "0" * 32


def route_line(
    interface="eth0",
    destination=ZERO,
    destination_prefix="00",
    source=ZERO,
    source_prefix="00",
    next_hop="fe800000000000000000000000000001",
    metric="00000400",
    refcnt="00000000",
    use="00000000",
    flags="00000003",
):
    return (
        f"{destination} {destination_prefix} {source} {source_prefix} "
        f"{next_hop} {metric} {refcnt} {use} {flags} {interface}\n"
    )


class IPv6DefaultRouteCheckTest(unittest.TestCase):
    def run_check(self, content, interface="eth0", *, env=None):
        with tempfile.TemporaryDirectory() as tmp:
            table = Path(tmp) / "ipv6_route"
            table.write_text(content, encoding="ascii")
            command = ["bash", str(SCRIPT)]
            if interface is not None:
                command.extend([interface, str(table)])
            merged_env = os.environ.copy()
            if env:
                merged_env.update(env)
            result = subprocess.run(
                command,
                text=True,
                capture_output=True,
                check=False,
                env=merged_env,
            )
            return result

    def test_up_default_route_is_ok(self):
        result = self.run_check(route_line(flags="00000003"))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "IRLIGHT_IPV6_DEFAULT_ROUTE status=OK route=default")

    def test_other_interface_does_not_satisfy_target(self):
        result = self.run_check(route_line(interface="eth1"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("reason=default_route_missing", result.stdout)

    def test_non_default_route_does_not_satisfy_target(self):
        result = self.run_check(route_line(destination="20010db8" + "0" * 24, destination_prefix="20"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("reason=default_route_missing", result.stdout)

    def test_source_specific_default_does_not_satisfy_generic_target(self):
        result = self.run_check(route_line(source="20010db8" + "0" * 24, source_prefix="20"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("reason=default_route_missing", result.stdout)

    def test_down_or_reject_default_is_critical(self):
        for flags in ("00000000", "00000201", "00000203"):
            with self.subTest(flags=flags):
                result = self.run_check(route_line(flags=flags))
                self.assertEqual(result.returncode, 2)
                self.assertIn("reason=default_route_unusable", result.stdout)

    def test_malformed_target_row_is_unknown(self):
        result = self.run_check(route_line(destination="xyz"))
        self.assertEqual(result.returncode, 3)
        self.assertIn("reason=invalid_route_table", result.stdout)

    def test_unattributable_malformed_row_is_unknown_without_valid_default(self):
        result = self.run_check("broken row\n" + route_line(interface="eth1"))
        self.assertEqual(result.returncode, 3)
        self.assertIn("reason=invalid_route_table", result.stdout)

    def test_valid_target_default_wins_over_other_malformed_row(self):
        result = self.run_check("broken row\n" + route_line())
        self.assertEqual(result.returncode, 0)
        self.assertIn("status=OK", result.stdout)

    def test_prefix_over_128_is_unknown(self):
        result = self.run_check(route_line(destination_prefix="81"))
        self.assertEqual(result.returncode, 3)
        self.assertIn("reason=invalid_route_table", result.stdout)

    def test_empty_table_means_missing_route(self):
        result = self.run_check("")
        self.assertEqual(result.returncode, 2)
        self.assertIn("reason=default_route_missing", result.stdout)

    def test_missing_or_invalid_target_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = Path(tmp) / "ipv6_route"
            table.write_text(route_line(), encoding="ascii")
            for target in (None, "bad target", "bad/name"):
                with self.subTest(target=target):
                    command = ["bash", str(SCRIPT)]
                    if target is not None:
                        command.extend([target, str(table)])
                    result = subprocess.run(command, text=True, capture_output=True, check=False)
                    self.assertEqual(result.returncode, 3)

    def test_environment_overrides_target_and_route_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = Path(tmp) / "ipv6_route"
            table.write_text(route_line(interface="ens3"), encoding="ascii")
            env = os.environ.copy()
            env.update({
                "IRLIGHT_NETWORK_INTERFACE": "ens3",
                "IRLIGHT_IPV6_ROUTE_TABLE": str(table),
            })
            result = subprocess.run(
                ["bash", str(SCRIPT)], text=True, capture_output=True, check=False, env=env
            )
            self.assertEqual(result.returncode, 0)
            self.assertIn("status=OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
