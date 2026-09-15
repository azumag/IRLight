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


def ipv4_route(interface: str = "eth0") -> str:
    return f"{interface}\t00000000\t0100000A\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"


def ipv6_route(interface: str = "eth0") -> str:
    return (
        f"{ZERO6} 00 {ZERO6} 00 fe800000000000000000000000000001 "
        f"00000400 00000000 00000000 00000003 {interface}\n"
    )


def netstat_record(*, listen_overflows: int = 0, listen_drops: int = 0) -> str:
    return (
        "TcpExt: SyncookiesSent ListenOverflows ListenDrops TCPTimeouts\n"
        f"TcpExt: 0 {listen_overflows} {listen_drops} 0\n"
    )


class NetworkEgressTcpListenPressureTest(unittest.TestCase):
    def run_check(
        self,
        *,
        mode: str | None = None,
        current: str | None = None,
        baseline: str | None = None,
        ipv6_content: str | None = None,
        listener_checker_body: str | None = None,
        interface_mode: str | None = None,
        interface_checker_body: str | None = None,
        udp_mode: str | None = None,
        udp_checker_body: str | None = None,
        tcp_mode: str | None = None,
        tcp_checker_body: str | None = None,
        timeout_seconds: str | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], float]:
        with tempfile.TemporaryDirectory(prefix="irlight-network-egress-listener-") as tmp:
            root = Path(tmp)
            scripts = root / "scripts"
            scripts.mkdir()
            (scripts / AGGREGATE.name).write_text(AGGREGATE.read_text(encoding="utf-8"), encoding="utf-8")
            for name in (
                "check-network-link-health.sh",
                "check-ipv4-default-route.sh",
                "check-ipv6-default-route.sh",
                "check-network-interface-errors.sh",
                "check-udp-snmp-errors.sh",
                "check-tcp-snmp-retransmits.sh",
                "check-tcp-listen-overflows.sh",
            ):
                target = scripts / name
                if name == "check-tcp-listen-overflows.sh" and listener_checker_body is not None:
                    target.write_text(listener_checker_body, encoding="utf-8")
                elif name == "check-network-interface-errors.sh" and interface_checker_body is not None:
                    target.write_text(interface_checker_body, encoding="utf-8")
                elif name == "check-udp-snmp-errors.sh" and udp_checker_body is not None:
                    target.write_text(udp_checker_body, encoding="utf-8")
                elif name == "check-tcp-snmp-retransmits.sh" and tcp_checker_body is not None:
                    target.write_text(tcp_checker_body, encoding="utf-8")
                else:
                    target.write_text((SCRIPTS / name).read_text(encoding="utf-8"), encoding="utf-8")

            interface_dir = root / "net" / "eth0"
            interface_dir.mkdir(parents=True)
            (interface_dir / "operstate").write_text("up\n", encoding="ascii")

            ipv4_table = root / "route"
            ipv4_table.write_text(IPV4_HEADER + ipv4_route(), encoding="ascii")
            ipv6_table = root / "ipv6_route"
            ipv6_table.write_text(ipv6_route() if ipv6_content is None else ipv6_content, encoding="ascii")

            current_path = root / "netstat.current"
            current_path.write_text(current or netstat_record(), encoding="utf-8")

            env = os.environ.copy()
            env["IRLIGHT_TCP_NETSTAT_PATH"] = str(current_path)
            if mode is not None:
                env["IRLIGHT_TCP_LISTEN_PRESSURE_MODE"] = mode
            if baseline is not None:
                baseline_path = root / "netstat.baseline"
                baseline_path.write_text(baseline, encoding="utf-8")
                env["IRLIGHT_TCP_NETSTAT_BASELINE_PATH"] = str(baseline_path)
            if interface_mode is not None:
                env["IRLIGHT_NETWORK_INTERFACE_ERRORS_MODE"] = interface_mode
            if udp_mode is not None:
                env["IRLIGHT_UDP_SNMP_ERRORS_MODE"] = udp_mode
            if tcp_mode is not None:
                env["IRLIGHT_TCP_SNMP_RETRANSMITS_MODE"] = tcp_mode
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

    def test_enabled_ok_and_warning_report_listener_field(self) -> None:
        baseline = netstat_record(listen_overflows=10, listen_drops=20)
        ok, _ = self.run_check(mode="enabled", current=baseline, baseline=baseline)
        self.assertEqual(ok.returncode, 0)
        self.assertEqual(
            ok.stdout.strip(),
            "IRLIGHT_NETWORK_EGRESS_HEALTH status=OK link_status=OK "
            "ipv4_route_status=OK ipv6_route_status=OK tcp_listen_pressure_status=OK family=dual",
        )

        warning, _ = self.run_check(
            mode="enabled",
            current=netstat_record(listen_overflows=11, listen_drops=22),
            baseline=baseline,
        )
        self.assertEqual(warning.returncode, 1)
        self.assertIn("status=WARNING", warning.stdout)
        self.assertIn("tcp_listen_pressure_status=WARNING", warning.stdout)

    def test_missing_baseline_and_invalid_mode_fail_closed(self) -> None:
        missing, _ = self.run_check(mode="enabled")
        self.assertEqual(missing.returncode, 3)
        self.assertIn("tcp_listen_pressure_status=UNKNOWN", missing.stdout)

        invalid, _ = self.run_check(mode="yes")
        self.assertEqual(invalid.returncode, 3)
        self.assertIn("tcp_listen_pressure_status=UNKNOWN", invalid.stdout)

    def test_confirmed_route_critical_wins_over_listener_warning_or_unknown(self) -> None:
        warning, _ = self.run_check(
            mode="enabled",
            current=netstat_record(listen_overflows=1),
            baseline=netstat_record(),
            ipv6_content="",
        )
        self.assertEqual(warning.returncode, 2)
        self.assertIn("status=CRITICAL", warning.stdout)
        self.assertIn("tcp_listen_pressure_status=WARNING", warning.stdout)

        unknown, _ = self.run_check(mode="enabled", ipv6_content="")
        self.assertEqual(unknown.returncode, 2)
        self.assertIn("status=CRITICAL", unknown.stdout)
        self.assertIn("tcp_listen_pressure_status=UNKNOWN", unknown.stdout)

    def test_all_optional_components_have_stable_order_and_severity(self) -> None:
        result, _ = self.run_check(
            mode="enabled",
            current=netstat_record(listen_drops=1),
            baseline=netstat_record(),
            interface_mode="enabled",
            interface_checker_body="#!/usr/bin/env bash\nexit 2\n",
            udp_mode="enabled",
            udp_checker_body="#!/usr/bin/env bash\nexit 1\n",
            tcp_mode="enabled",
            tcp_checker_body="#!/usr/bin/env bash\nexit 1\n",
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_NETWORK_EGRESS_HEALTH status=CRITICAL link_status=OK "
            "ipv4_route_status=OK ipv6_route_status=OK interface_errors_status=CRITICAL "
            "udp_snmp_errors_status=WARNING tcp_snmp_retransmits_status=WARNING "
            "tcp_listen_pressure_status=WARNING family=dual",
        )

    def test_listener_component_timeout_becomes_unknown(self) -> None:
        result, elapsed = self.run_check(
            mode="enabled",
            baseline=netstat_record(),
            listener_checker_body="#!/usr/bin/env bash\nsleep 5\nexit 0\n",
            timeout_seconds="1",
        )
        self.assertEqual(result.returncode, 3)
        self.assertLess(elapsed, 4)
        self.assertIn("status=UNKNOWN", result.stdout)
        self.assertIn("tcp_listen_pressure_status=UNKNOWN", result.stdout)


if __name__ == "__main__":
    unittest.main()
