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
ISOLATED_ENV_VARS = (
    "IRLIGHT_NETWORK_INTERFACE_ERRORS_MODE",
    "IRLIGHT_UDP_SNMP_ERRORS_MODE",
    "IRLIGHT_TCP_SNMP_RETRANSMITS_MODE",
    "IRLIGHT_TCP_LISTEN_PRESSURE_MODE",
    "IRLIGHT_TCP_ESTABLISHED_RESETS_MODE",
    "IRLIGHT_TCP_ATTEMPT_FAILS_MODE",
    "IRLIGHT_CONNTRACK_PRESSURE_MODE",
    "IRLIGHT_CONNTRACK_WARNING_PERCENT",
    "IRLIGHT_CONNTRACK_CRITICAL_PERCENT",
    "IRLIGHT_NETWORK_COMPONENT_TIMEOUT_SECONDS",
)


def ipv4_route(interface: str = "eth0") -> str:
    return f"{interface}\t00000000\t0100000A\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"


def ipv6_route(interface: str = "eth0") -> str:
    return (
        f"{ZERO6} 00 {ZERO6} 00 fe800000000000000000000000000001 "
        f"00000400 00000000 00000000 00000003 {interface}\n"
    )


class NetworkEgressConntrackPressureTest(unittest.TestCase):
    def run_check(
        self,
        *,
        mode: str | None = None,
        count: str | None = "10\n",
        maximum: str | None = "100\n",
        ipv6_content: str | None = None,
        conntrack_checker_body: str | None = None,
        interface_mode: str | None = None,
        interface_checker_body: str | None = None,
        udp_mode: str | None = None,
        udp_checker_body: str | None = None,
        retransmit_mode: str | None = None,
        retransmit_checker_body: str | None = None,
        listener_mode: str | None = None,
        listener_checker_body: str | None = None,
        established_mode: str | None = None,
        established_checker_body: str | None = None,
        attempt_mode: str | None = None,
        attempt_checker_body: str | None = None,
        timeout_seconds: str | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], float]:
        with tempfile.TemporaryDirectory(prefix="irlight-network-egress-conntrack-") as tmp:
            root = Path(tmp)
            scripts = root / "scripts"
            scripts.mkdir()
            (scripts / AGGREGATE.name).write_text(AGGREGATE.read_text(encoding="utf-8"), encoding="utf-8")

            checker_bodies = {
                "check-network-interface-errors.sh": interface_checker_body,
                "check-udp-snmp-errors.sh": udp_checker_body,
                "check-tcp-snmp-retransmits.sh": retransmit_checker_body,
                "check-tcp-listen-overflows.sh": listener_checker_body,
                "check-tcp-snmp-established-resets.sh": established_checker_body,
                "check-tcp-snmp-attempt-fails.sh": attempt_checker_body,
                "check-conntrack-pressure.sh": conntrack_checker_body,
            }
            for name in (
                "check-network-link-health.sh",
                "check-ipv4-default-route.sh",
                "check-ipv6-default-route.sh",
                *checker_bodies.keys(),
            ):
                target = scripts / name
                body = checker_bodies.get(name)
                if body is not None:
                    target.write_text(body, encoding="utf-8")
                else:
                    target.write_text((SCRIPTS / name).read_text(encoding="utf-8"), encoding="utf-8")

            lib_dir = scripts / "lib"
            lib_dir.mkdir()
            (lib_dir / "scalar-pressure-common.sh").write_text(
                (SCRIPTS / "lib" / "scalar-pressure-common.sh").read_text(encoding="utf-8"),
                encoding="utf-8",
            )

            interface_dir = root / "net" / "eth0"
            interface_dir.mkdir(parents=True)
            (interface_dir / "operstate").write_text("up\n", encoding="ascii")

            ipv4_table = root / "route"
            ipv4_table.write_text(IPV4_HEADER + ipv4_route(), encoding="ascii")
            ipv6_table = root / "ipv6_route"
            ipv6_table.write_text(ipv6_route() if ipv6_content is None else ipv6_content, encoding="ascii")

            count_path = root / "nf_conntrack_count"
            max_path = root / "nf_conntrack_max"
            if count is not None:
                count_path.write_text(count, encoding="ascii")
            if maximum is not None:
                max_path.write_text(maximum, encoding="ascii")

            env = os.environ.copy()
            for key in ISOLATED_ENV_VARS:
                env.pop(key, None)
            env["IRLIGHT_CONNTRACK_COUNT_PATH"] = str(count_path)
            env["IRLIGHT_CONNTRACK_MAX_PATH"] = str(max_path)
            if mode is not None:
                env["IRLIGHT_CONNTRACK_PRESSURE_MODE"] = mode
            if interface_mode is not None:
                env["IRLIGHT_NETWORK_INTERFACE_ERRORS_MODE"] = interface_mode
            if udp_mode is not None:
                env["IRLIGHT_UDP_SNMP_ERRORS_MODE"] = udp_mode
            if retransmit_mode is not None:
                env["IRLIGHT_TCP_SNMP_RETRANSMITS_MODE"] = retransmit_mode
            if listener_mode is not None:
                env["IRLIGHT_TCP_LISTEN_PRESSURE_MODE"] = listener_mode
            if established_mode is not None:
                env["IRLIGHT_TCP_ESTABLISHED_RESETS_MODE"] = established_mode
            if attempt_mode is not None:
                env["IRLIGHT_TCP_ATTEMPT_FAILS_MODE"] = attempt_mode
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

    def test_enabled_maps_ok_warning_and_critical(self) -> None:
        ok, _ = self.run_check(mode="enabled", count="10\n", maximum="100\n")
        self.assertEqual(ok.returncode, 0)
        self.assertIn("conntrack_pressure_status=OK", ok.stdout)

        warning, _ = self.run_check(mode="enabled", count="80\n", maximum="100\n")
        self.assertEqual(warning.returncode, 1)
        self.assertIn("status=WARNING", warning.stdout)
        self.assertIn("conntrack_pressure_status=WARNING", warning.stdout)

        critical, _ = self.run_check(mode="enabled", count="90\n", maximum="100\n")
        self.assertEqual(critical.returncode, 2)
        self.assertIn("status=CRITICAL", critical.stdout)
        self.assertIn("conntrack_pressure_status=CRITICAL", critical.stdout)

    def test_missing_current_invalid_mode_timeout_and_exit_fail_closed(self) -> None:
        missing, _ = self.run_check(mode="enabled", count=None)
        self.assertEqual(missing.returncode, 3)
        self.assertIn("conntrack_pressure_status=UNKNOWN", missing.stdout)

        invalid_mode, _ = self.run_check(mode="yes")
        self.assertEqual(invalid_mode.returncode, 3)
        self.assertIn("conntrack_pressure_status=UNKNOWN", invalid_mode.stdout)

        invalid_timeout, _ = self.run_check(mode="enabled", timeout_seconds="0")
        self.assertEqual(invalid_timeout.returncode, 3)
        self.assertIn("conntrack_pressure_status=UNKNOWN", invalid_timeout.stdout)

        abnormal_exit, _ = self.run_check(
            mode="enabled",
            conntrack_checker_body="#!/usr/bin/env bash\nexit 9\n",
        )
        self.assertEqual(abnormal_exit.returncode, 3)
        self.assertIn("conntrack_pressure_status=UNKNOWN", abnormal_exit.stdout)

    def test_confirmed_route_critical_wins_over_conntrack_warning_or_unknown(self) -> None:
        warning, _ = self.run_check(mode="enabled", count="80\n", ipv6_content="")
        self.assertEqual(warning.returncode, 2)
        self.assertIn("status=CRITICAL", warning.stdout)
        self.assertIn("conntrack_pressure_status=WARNING", warning.stdout)

        unknown, _ = self.run_check(mode="enabled", count=None, ipv6_content="")
        self.assertEqual(unknown.returncode, 2)
        self.assertIn("status=CRITICAL", unknown.stdout)
        self.assertIn("conntrack_pressure_status=UNKNOWN", unknown.stdout)

    def test_all_optional_components_have_stable_order_and_severity(self) -> None:
        result, _ = self.run_check(
            mode="enabled",
            conntrack_checker_body="#!/usr/bin/env bash\nexit 1\n",
            interface_mode="enabled",
            interface_checker_body="#!/usr/bin/env bash\nexit 2\n",
            udp_mode="enabled",
            udp_checker_body="#!/usr/bin/env bash\nexit 1\n",
            retransmit_mode="enabled",
            retransmit_checker_body="#!/usr/bin/env bash\nexit 1\n",
            listener_mode="enabled",
            listener_checker_body="#!/usr/bin/env bash\nexit 1\n",
            established_mode="enabled",
            established_checker_body="#!/usr/bin/env bash\nexit 1\n",
            attempt_mode="enabled",
            attempt_checker_body="#!/usr/bin/env bash\nexit 1\n",
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_NETWORK_EGRESS_HEALTH status=CRITICAL link_status=OK "
            "ipv4_route_status=OK ipv6_route_status=OK interface_errors_status=CRITICAL "
            "udp_snmp_errors_status=WARNING tcp_snmp_retransmits_status=WARNING "
            "tcp_listen_pressure_status=WARNING tcp_established_resets_status=WARNING "
            "tcp_attempt_fails_status=WARNING conntrack_pressure_status=WARNING family=dual",
        )

    def test_conntrack_component_timeout_becomes_unknown(self) -> None:
        result, elapsed = self.run_check(
            mode="enabled",
            conntrack_checker_body="#!/usr/bin/env bash\nsleep 5\nexit 0\n",
            timeout_seconds="1",
        )
        self.assertEqual(result.returncode, 3)
        self.assertLess(elapsed, 4)
        self.assertIn("status=UNKNOWN", result.stdout)
        self.assertIn("conntrack_pressure_status=UNKNOWN", result.stdout)


if __name__ == "__main__":
    unittest.main()
