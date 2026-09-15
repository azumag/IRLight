from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
AGGREGATE = SCRIPTS / "check-network-egress-health.sh"
IPV4_HEADER = "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
ZERO6 = "0" * 32
COUNTERS = ("rx_errors", "tx_errors", "rx_dropped", "tx_dropped")


def ipv4_route(interface: str = "eth0") -> str:
    return f"{interface}\t00000000\t0100000A\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"


def ipv6_route(interface: str = "eth0") -> str:
    return (
        f"{ZERO6} 00 {ZERO6} 00 fe800000000000000000000000000001 "
        f"00000400 00000000 00000000 00000003 {interface}\n"
    )


def write_stats(directory: Path, values: dict[str, int | str]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for counter in COUNTERS:
        value = values.get(counter, 10)
        (directory / counter).write_text(f"{value}\n", encoding="ascii")


class NetworkEgressInterfaceErrorsTest(unittest.TestCase):
    def run_check(
        self,
        *,
        mode: str | None = None,
        current: dict[str, int | str] | None = None,
        baseline: dict[str, int | str] | None = None,
        ipv6_content: str | None = None,
        interface_checker_body: str | None = None,
        timeout_seconds: str | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], float]:
        with tempfile.TemporaryDirectory(prefix="irlight-network-egress-interface-") as tmp:
            root = Path(tmp)
            scripts = root / "scripts"
            scripts.mkdir()
            (scripts / AGGREGATE.name).write_text(AGGREGATE.read_text(encoding="utf-8"), encoding="utf-8")
            for name in (
                "check-network-link-health.sh",
                "check-ipv4-default-route.sh",
                "check-ipv6-default-route.sh",
                "check-network-interface-errors.sh",
            ):
                target = scripts / name
                if name == "check-network-interface-errors.sh" and interface_checker_body is not None:
                    target.write_text(interface_checker_body, encoding="utf-8")
                else:
                    target.write_text((SCRIPTS / name).read_text(encoding="utf-8"), encoding="utf-8")

            interface_dir = root / "net" / "eth0"
            interface_dir.mkdir(parents=True)
            (interface_dir / "operstate").write_text("up\n", encoding="ascii")
            write_stats(interface_dir / "statistics", current or {})

            ipv4_table = root / "route"
            ipv4_table.write_text(IPV4_HEADER + ipv4_route(), encoding="ascii")
            ipv6_table = root / "ipv6_route"
            ipv6_table.write_text(ipv6_route() if ipv6_content is None else ipv6_content, encoding="ascii")

            env = os.environ.copy()
            if mode is not None:
                env["IRLIGHT_NETWORK_INTERFACE_ERRORS_MODE"] = mode
            if baseline is not None:
                baseline_dir = root / "baseline"
                write_stats(baseline_dir, baseline)
                env["IRLIGHT_NETWORK_STATS_BASELINE_DIR"] = str(baseline_dir)
            if timeout_seconds is not None:
                env["IRLIGHT_NETWORK_COMPONENT_TIMEOUT_SECONDS"] = timeout_seconds

            started = time.monotonic()
            result = subprocess.run(
                [
                    "bash",
                    str(scripts / AGGREGATE.name),
                    "eth0",
                    "dual",
                    str(interface_dir),
                    str(ipv4_table),
                    str(ipv6_table),
                ],
                text=True,
                capture_output=True,
                check=False,
                env=env,
                timeout=6,
            )
            return result, time.monotonic() - started

    def test_disabled_mode_preserves_existing_stdout_and_exit_code(self) -> None:
        result, _ = self.run_check()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_NETWORK_EGRESS_HEALTH status=OK link_status=OK "
            "ipv4_route_status=OK ipv6_route_status=OK family=dual",
        )

    def test_enabled_ok_preserves_interface_only_field_order(self) -> None:
        result, _ = self.run_check(mode="enabled", baseline={})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_NETWORK_EGRESS_HEALTH status=OK link_status=OK "
            "ipv4_route_status=OK ipv6_route_status=OK interface_errors_status=OK family=dual",
        )

    def test_drop_delta_propagates_warning(self) -> None:
        result, _ = self.run_check(
            mode="enabled",
            current={"rx_dropped": 11},
            baseline={},
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("status=WARNING", result.stdout)
        self.assertIn("interface_errors_status=WARNING", result.stdout)

    def test_error_delta_propagates_critical_and_wins_over_route_unknown(self) -> None:
        result, _ = self.run_check(
            mode="enabled",
            current={"tx_errors": 11},
            baseline={},
            ipv6_content="broken row\n",
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("ipv6_route_status=UNKNOWN", result.stdout)
        self.assertIn("interface_errors_status=CRITICAL", result.stdout)

    def test_counter_reset_fails_closed(self) -> None:
        result, _ = self.run_check(
            mode="enabled",
            current={"rx_errors": 9},
            baseline={},
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("status=UNKNOWN", result.stdout)
        self.assertIn("interface_errors_status=UNKNOWN", result.stdout)

    def test_missing_or_invalid_baseline_fails_closed(self) -> None:
        missing, _ = self.run_check(mode="enabled")
        self.assertEqual(missing.returncode, 3)
        self.assertIn("interface_errors_status=UNKNOWN", missing.stdout)

        invalid, _ = self.run_check(mode="enabled", baseline={"rx_errors": "broken"})
        self.assertEqual(invalid.returncode, 3)
        self.assertIn("interface_errors_status=UNKNOWN", invalid.stdout)

    def test_invalid_mode_fails_closed_without_hiding_confirmed_critical(self) -> None:
        unknown, _ = self.run_check(mode="yes")
        self.assertEqual(unknown.returncode, 3)
        self.assertIn("interface_errors_status=UNKNOWN", unknown.stdout)

        critical, _ = self.run_check(mode="yes", ipv6_content="")
        self.assertEqual(critical.returncode, 2)
        self.assertIn("status=CRITICAL", critical.stdout)
        self.assertIn("interface_errors_status=UNKNOWN", critical.stdout)

    def test_interface_counter_component_timeout_becomes_unknown(self) -> None:
        result, elapsed = self.run_check(
            mode="enabled",
            baseline={},
            interface_checker_body="#!/usr/bin/env bash\nsleep 5\nexit 0\n",
            timeout_seconds="1",
        )
        self.assertEqual(result.returncode, 3)
        self.assertLess(elapsed, 4)
        self.assertIn("status=UNKNOWN", result.stdout)
        self.assertIn("interface_errors_status=UNKNOWN", result.stdout)


if __name__ == "__main__":
    unittest.main()
