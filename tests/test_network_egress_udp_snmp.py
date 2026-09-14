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
UDP_FIELDS = (
    "InDatagrams",
    "NoPorts",
    "InErrors",
    "OutDatagrams",
    "RcvbufErrors",
    "SndbufErrors",
    "InCsumErrors",
    "IgnoredMulti",
    "MemErrors",
)


def ipv4_route(interface: str = "eth0") -> str:
    return f"{interface}\t00000000\t0100000A\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"


def ipv6_route(interface: str = "eth0") -> str:
    return (
        f"{ZERO6} 00 {ZERO6} 00 fe800000000000000000000000000001 "
        f"00000400 00000000 00000000 00000003 {interface}\n"
    )


def snmp_record(**overrides: int | str) -> str:
    values = {field: "0" for field in UDP_FIELDS}
    values.update({field: str(value) for field, value in overrides.items()})
    return (
        "Ip: Forwarding DefaultTTL\n"
        "Ip: 2 64\n"
        f"Udp: {' '.join(UDP_FIELDS)}\n"
        f"Udp: {' '.join(values[field] for field in UDP_FIELDS)}\n"
    )


class NetworkEgressUdpSnmpTest(unittest.TestCase):
    def run_check(
        self,
        *,
        mode: str | None = None,
        current: str | None = None,
        baseline: str | None = None,
        ipv6_content: str | None = None,
        udp_checker_body: str | None = None,
        timeout_seconds: str | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], float]:
        with tempfile.TemporaryDirectory(prefix="irlight-network-egress-udp-") as tmp:
            root = Path(tmp)
            scripts = root / "scripts"
            scripts.mkdir()
            (scripts / AGGREGATE.name).write_text(AGGREGATE.read_text(encoding="utf-8"), encoding="utf-8")
            for name in (
                "check-network-link-health.sh",
                "check-ipv4-default-route.sh",
                "check-ipv6-default-route.sh",
                "check-udp-snmp-errors.sh",
            ):
                target = scripts / name
                if name == "check-udp-snmp-errors.sh" and udp_checker_body is not None:
                    target.write_text(udp_checker_body, encoding="utf-8")
                else:
                    target.write_text((SCRIPTS / name).read_text(encoding="utf-8"), encoding="utf-8")

            interface_dir = root / "net" / "eth0"
            interface_dir.mkdir(parents=True)
            (interface_dir / "operstate").write_text("up\n", encoding="ascii")

            ipv4_table = root / "route"
            ipv4_table.write_text(IPV4_HEADER + ipv4_route(), encoding="ascii")
            ipv6_table = root / "ipv6_route"
            ipv6_table.write_text(ipv6_route() if ipv6_content is None else ipv6_content, encoding="ascii")

            current_path = root / "snmp.current"
            current_path.write_text(current or snmp_record(), encoding="utf-8")

            env = os.environ.copy()
            env["IRLIGHT_UDP_SNMP_PATH"] = str(current_path)
            if mode is not None:
                env["IRLIGHT_UDP_SNMP_ERRORS_MODE"] = mode
            if baseline is not None:
                baseline_path = root / "snmp.baseline"
                baseline_path.write_text(baseline, encoding="utf-8")
                env["IRLIGHT_UDP_SNMP_BASELINE_PATH"] = str(baseline_path)
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

    def test_enabled_ok_is_included_in_aggregate(self) -> None:
        baseline = snmp_record(InErrors=3, RcvbufErrors=4, SndbufErrors=5, InCsumErrors=6)
        result, _ = self.run_check(mode="enabled", current=baseline, baseline=baseline)
        self.assertEqual(result.returncode, 0)
        self.assertIn("status=OK", result.stdout)
        self.assertIn("udp_snmp_errors_status=OK", result.stdout)

    def test_udp_error_delta_propagates_warning(self) -> None:
        result, _ = self.run_check(
            mode="enabled",
            current=snmp_record(RcvbufErrors=11),
            baseline=snmp_record(RcvbufErrors=10),
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("status=WARNING", result.stdout)
        self.assertIn("udp_snmp_errors_status=WARNING", result.stdout)

    def test_missing_or_malformed_baseline_fails_closed(self) -> None:
        missing, _ = self.run_check(mode="enabled")
        self.assertEqual(missing.returncode, 3)
        self.assertIn("udp_snmp_errors_status=UNKNOWN", missing.stdout)

        malformed, _ = self.run_check(mode="enabled", baseline="broken\n")
        self.assertEqual(malformed.returncode, 3)
        self.assertIn("udp_snmp_errors_status=UNKNOWN", malformed.stdout)

    def test_confirmed_route_critical_wins_over_udp_unknown_or_warning(self) -> None:
        unknown, _ = self.run_check(mode="enabled", ipv6_content="")
        self.assertEqual(unknown.returncode, 2)
        self.assertIn("status=CRITICAL", unknown.stdout)
        self.assertIn("udp_snmp_errors_status=UNKNOWN", unknown.stdout)

        warning, _ = self.run_check(
            mode="enabled",
            current=snmp_record(InErrors=1),
            baseline=snmp_record(),
            ipv6_content="",
        )
        self.assertEqual(warning.returncode, 2)
        self.assertIn("status=CRITICAL", warning.stdout)
        self.assertIn("udp_snmp_errors_status=WARNING", warning.stdout)

    def test_invalid_mode_fails_closed_without_hiding_confirmed_critical(self) -> None:
        unknown, _ = self.run_check(mode="yes")
        self.assertEqual(unknown.returncode, 3)
        self.assertIn("udp_snmp_errors_status=UNKNOWN", unknown.stdout)

        critical, _ = self.run_check(mode="yes", ipv6_content="")
        self.assertEqual(critical.returncode, 2)
        self.assertIn("status=CRITICAL", critical.stdout)
        self.assertIn("udp_snmp_errors_status=UNKNOWN", critical.stdout)

    def test_udp_component_timeout_becomes_unknown(self) -> None:
        result, elapsed = self.run_check(
            mode="enabled",
            baseline=snmp_record(),
            udp_checker_body="#!/usr/bin/env bash\nsleep 5\nexit 0\n",
            timeout_seconds="1",
        )
        self.assertEqual(result.returncode, 3)
        self.assertLess(elapsed, 4)
        self.assertIn("status=UNKNOWN", result.stdout)
        self.assertIn("udp_snmp_errors_status=UNKNOWN", result.stdout)


if __name__ == "__main__":
    unittest.main()
