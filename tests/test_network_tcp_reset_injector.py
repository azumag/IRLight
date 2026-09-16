from __future__ import annotations

import importlib.util
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import call, patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "network-tcp-reset-injector.py"
SPEC = importlib.util.spec_from_file_location("network_tcp_reset_injector", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class NetworkTcpResetInjectorTest(unittest.TestCase):
    def test_ipv4_plan_is_namespaced_and_exact(self) -> None:
        plan = MODULE.build_tcp_reset_plan(
            namespace="irlight-qa",
            destination="192.0.2.10",
            port=1935,
            rule_id="rtmp-reset-01",
            duration_seconds=30,
        )

        self.assertEqual(plan.destination, "192.0.2.10")
        self.assertEqual(plan.duration_seconds, 30)
        self.assertEqual(
            plan.apply_argv,
            (
                "ip",
                "netns",
                "exec",
                "irlight-qa",
                "iptables",
                "-w",
                "2",
                "-I",
                "OUTPUT",
                "1",
                "-p",
                "tcp",
                "-d",
                "192.0.2.10",
                "--dport",
                "1935",
                "-m",
                "comment",
                "--comment",
                "irlight-qa-tcp-reset:rtmp-reset-01",
                "-j",
                "REJECT",
                "--reject-with",
                "tcp-reset",
            ),
        )
        self.assertEqual(plan.cleanup_argv[0:5], ("ip", "netns", "exec", "irlight-qa", "iptables"))
        self.assertIn("-D", plan.cleanup_argv)
        self.assertNotIn("-I", plan.cleanup_argv)

    def test_ipv6_plan_uses_ip6tables_and_normalizes_address(self) -> None:
        plan = MODULE.build_tcp_reset_plan(
            namespace="irlight-qa",
            destination="2001:0db8:0000:0000:0000:0000:0000:0010",
            port=443,
            rule_id="rtmps-v6",
            duration_seconds=10,
        )

        self.assertEqual(plan.destination, "2001:db8::10")
        self.assertEqual(plan.apply_argv[4], "ip6tables")
        self.assertIn("2001:db8::10", plan.apply_argv)

    def test_hostname_and_non_unicast_destinations_are_rejected(self) -> None:
        for destination in ("example.invalid", "0.0.0.0", "224.0.0.1", "::", "ff02::1"):
            with self.subTest(destination=destination):
                with self.assertRaises(MODULE.TcpResetPlanError):
                    MODULE.build_tcp_reset_plan(
                        namespace="irlight-qa",
                        destination=destination,
                        port=1935,
                        rule_id="case-1",
                        duration_seconds=30,
                    )

    def test_namespace_rule_port_and_duration_are_strict(self) -> None:
        invalid_cases = (
            {"namespace": "../host", "destination": "192.0.2.10", "port": 1935, "rule_id": "case-1", "duration_seconds": 30},
            {"namespace": "irlight-qa", "destination": "192.0.2.10", "port": True, "rule_id": "case-1", "duration_seconds": 30},
            {"namespace": "irlight-qa", "destination": "192.0.2.10", "port": 0, "rule_id": "case-1", "duration_seconds": 30},
            {"namespace": "irlight-qa", "destination": "192.0.2.10", "port": 1935, "rule_id": "bad rule", "duration_seconds": 30},
            {"namespace": "irlight-qa", "destination": "192.0.2.10", "port": 1935, "rule_id": "case-1", "duration_seconds": True},
            {"namespace": "irlight-qa", "destination": "192.0.2.10", "port": 1935, "rule_id": "case-1", "duration_seconds": 5},
        )
        for kwargs in invalid_cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(MODULE.TcpResetPlanError):
                    MODULE.build_tcp_reset_plan(**kwargs)

    def test_plan_command_is_read_only(self) -> None:
        output = io.StringIO()
        with patch.object(MODULE, "_run") as run, redirect_stdout(output):
            result = MODULE.main(
                [
                    "plan",
                    "--namespace",
                    "irlight-qa",
                    "--destination",
                    "192.0.2.10",
                    "--port",
                    "1935",
                    "--rule-id",
                    "rtmp-reset-01",
                    "--duration",
                    "30",
                    "--json",
                ]
            )

        self.assertEqual(result, 0)
        run.assert_not_called()
        self.assertIn('"destination": "192.0.2.10"', output.getvalue())
        self.assertIn('"duration_seconds": 30', output.getvalue())

    def test_programmatic_apply_requires_disposable_namespace_acknowledgement(self) -> None:
        with patch.object(MODULE, "_run") as run:
            with self.assertRaisesRegex(
                MODULE.TcpResetPlanError, "acknowledgement is required"
            ):
                MODULE.apply_tcp_reset(
                    namespace="irlight-qa",
                    destination="192.0.2.10",
                    port=1935,
                    rule_id="case-1",
                    duration_seconds=30,
                    confirm_disposable_namespace=False,
                )
        run.assert_not_called()

    def test_apply_sleeps_for_bounded_duration_then_cleans_up(self) -> None:
        plan = MODULE.build_tcp_reset_plan(
            namespace="irlight-qa",
            destination="192.0.2.10",
            port=1935,
            rule_id="case-1",
            duration_seconds=30,
        )
        with (
            patch.object(MODULE, "_run") as run,
            patch.object(MODULE.time, "sleep") as sleep,
        ):
            result = MODULE.apply_tcp_reset(
                namespace="irlight-qa",
                destination="192.0.2.10",
                port=1935,
                rule_id="case-1",
                duration_seconds=30,
                confirm_disposable_namespace=True,
            )

        self.assertEqual(result, 0)
        self.assertEqual(run.call_args_list, [call(plan.apply_argv), call(plan.cleanup_argv)])
        sleep.assert_called_once_with(30)

    def test_cleanup_is_attempted_after_apply_failure(self) -> None:
        plan = MODULE.build_tcp_reset_plan(
            namespace="irlight-qa",
            destination="192.0.2.10",
            port=1935,
            rule_id="case-1",
            duration_seconds=30,
        )
        with patch.object(
            MODULE,
            "_run",
            side_effect=(RuntimeError("apply failed"), None),
        ) as run:
            with self.assertRaisesRegex(RuntimeError, "TCP reset apply failed"):
                MODULE.apply_tcp_reset(
                    namespace="irlight-qa",
                    destination="192.0.2.10",
                    port=1935,
                    rule_id="case-1",
                    duration_seconds=30,
                    confirm_disposable_namespace=True,
                )

        self.assertEqual(run.call_args_list, [call(plan.apply_argv), call(plan.cleanup_argv)])

    def test_cleanup_failure_is_not_reported_as_success(self) -> None:
        with (
            patch.object(MODULE, "_run", side_effect=(None, RuntimeError("cleanup failed"))),
            patch.object(MODULE.time, "sleep"),
        ):
            with self.assertRaisesRegex(RuntimeError, "TCP reset cleanup failed"):
                MODULE.apply_tcp_reset(
                    namespace="irlight-qa",
                    destination="192.0.2.10",
                    port=1935,
                    rule_id="case-1",
                    duration_seconds=30,
                    confirm_disposable_namespace=True,
                )

    def test_clear_requires_acknowledgement_and_uses_exact_cleanup_rule(self) -> None:
        plan = MODULE.build_tcp_reset_plan(
            namespace="irlight-qa",
            destination="192.0.2.10",
            port=1935,
            rule_id="case-1",
            duration_seconds=None,
        )
        with patch.object(MODULE, "_run") as run:
            MODULE.clear_tcp_reset(
                namespace="irlight-qa",
                destination="192.0.2.10",
                port=1935,
                rule_id="case-1",
                confirm_disposable_namespace=True,
            )
        run.assert_called_once_with(plan.cleanup_argv)


if __name__ == "__main__":
    unittest.main()
