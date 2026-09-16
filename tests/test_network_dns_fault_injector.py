from __future__ import annotations

import importlib.util
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import call, patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "network-dns-fault-injector.py"
SPEC = importlib.util.spec_from_file_location("network_dns_fault_injector", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class NetworkDnsFaultInjectorTest(unittest.TestCase):
    def test_ipv4_plan_blocks_udp_and_tcp_dns_inside_namespace(self) -> None:
        plan = MODULE.build_dns_fault_plan(
            namespace="irlight-qa",
            resolver="192.0.2.53",
            rule_id="dns-outage-01",
            duration_seconds=30,
        )

        self.assertEqual(plan.resolver, "192.0.2.53")
        self.assertEqual(plan.duration_seconds, 30)
        self.assertEqual(len(plan.check_argvs), 2)
        self.assertEqual(len(plan.apply_argvs), 2)
        self.assertEqual(len(plan.cleanup_argvs), 2)
        self.assertEqual(
            plan.apply_argvs[0],
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
                "udp",
                "-d",
                "192.0.2.53",
                "--dport",
                "53",
                "-m",
                "comment",
                "--comment",
                "irlight-qa-dns-fault:dns-outage-01:udp",
                "-j",
                "REJECT",
            ),
        )
        self.assertIn("-C", plan.check_argvs[0])
        self.assertIn("tcp", plan.apply_argvs[1])
        self.assertIn("-D", plan.cleanup_argvs[0])
        self.assertNotIn("-I", plan.cleanup_argvs[0])

    def test_ipv6_plan_uses_ip6tables_and_normalizes_resolver(self) -> None:
        plan = MODULE.build_dns_fault_plan(
            namespace="irlight-qa",
            resolver="2001:0db8:0000:0000:0000:0000:0000:0053",
            rule_id="dns-v6",
            duration_seconds=10,
        )

        self.assertEqual(plan.resolver, "2001:db8::53")
        self.assertEqual(plan.apply_argvs[0][4], "ip6tables")
        self.assertIn("2001:db8::53", plan.apply_argvs[0])

    def test_hostname_and_non_unicast_resolvers_are_rejected(self) -> None:
        for resolver in ("resolver.invalid", "0.0.0.0", "224.0.0.1", "::", "ff02::1"):
            with self.subTest(resolver=resolver):
                with self.assertRaises(MODULE.DnsFaultPlanError):
                    MODULE.build_dns_fault_plan(
                        namespace="irlight-qa",
                        resolver=resolver,
                        rule_id="case-1",
                        duration_seconds=30,
                    )

    def test_namespace_rule_and_duration_are_strict(self) -> None:
        invalid_cases = (
            {"namespace": "../host", "resolver": "192.0.2.53", "rule_id": "case-1", "duration_seconds": 30},
            {"namespace": "irlight-qa", "resolver": "192.0.2.53", "rule_id": "bad rule", "duration_seconds": 30},
            {"namespace": "irlight-qa", "resolver": "192.0.2.53", "rule_id": "case-1", "duration_seconds": True},
            {"namespace": "irlight-qa", "resolver": "192.0.2.53", "rule_id": "case-1", "duration_seconds": 5},
        )
        for kwargs in invalid_cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(MODULE.DnsFaultPlanError):
                    MODULE.build_dns_fault_plan(**kwargs)

    def test_plan_command_is_read_only(self) -> None:
        output = io.StringIO()
        with patch.object(MODULE.subprocess, "run") as run, redirect_stdout(output):
            result = MODULE.main(
                [
                    "plan",
                    "--namespace",
                    "irlight-qa",
                    "--resolver",
                    "192.0.2.53",
                    "--rule-id",
                    "dns-outage-01",
                    "--duration",
                    "30",
                    "--json",
                ]
            )

        self.assertEqual(result, 0)
        run.assert_not_called()
        self.assertIn('"resolver": "192.0.2.53"', output.getvalue())
        self.assertIn('"duration_seconds": 30', output.getvalue())

    def test_programmatic_apply_requires_disposable_namespace_acknowledgement(self) -> None:
        with patch.object(MODULE.subprocess, "run") as run:
            with self.assertRaisesRegex(
                MODULE.DnsFaultPlanError, "acknowledgement is required"
            ):
                MODULE.apply_dns_fault(
                    namespace="irlight-qa",
                    resolver="192.0.2.53",
                    rule_id="case-1",
                    duration_seconds=30,
                    confirm_disposable_namespace=False,
                )
        run.assert_not_called()

    def test_preflight_command_error_does_not_mutate_firewall(self) -> None:
        with (
            patch.object(MODULE, "_run_status", return_value=2) as run_status,
            patch.object(MODULE, "_run") as run,
        ):
            with self.assertRaisesRegex(RuntimeError, "preflight check"):
                MODULE.apply_dns_fault(
                    namespace="irlight-qa",
                    resolver="192.0.2.53",
                    rule_id="case-1",
                    duration_seconds=30,
                    confirm_disposable_namespace=True,
                )

        run_status.assert_called_once()
        run.assert_not_called()

    def test_apply_blocks_both_protocols_then_cleans_up_in_reverse_order(self) -> None:
        plan = MODULE.build_dns_fault_plan(
            namespace="irlight-qa",
            resolver="192.0.2.53",
            rule_id="case-1",
            duration_seconds=30,
        )
        with (
            patch.object(MODULE, "_rule_exists", return_value=False),
            patch.object(MODULE, "_run") as run,
            patch.object(MODULE.time, "sleep") as sleep,
        ):
            result = MODULE.apply_dns_fault(
                namespace="irlight-qa",
                resolver="192.0.2.53",
                rule_id="case-1",
                duration_seconds=30,
                confirm_disposable_namespace=True,
            )

        self.assertEqual(result, 0)
        self.assertEqual(
            run.call_args_list,
            [
                call(plan.apply_argvs[0]),
                call(plan.apply_argvs[1]),
                call(plan.cleanup_argvs[1]),
                call(plan.cleanup_argvs[0]),
            ],
        )
        sleep.assert_called_once_with(30)

    def test_partial_apply_failure_attempts_cleanup_for_every_apply_attempt(self) -> None:
        plan = MODULE.build_dns_fault_plan(
            namespace="irlight-qa",
            resolver="192.0.2.53",
            rule_id="case-1",
            duration_seconds=30,
        )
        with (
            patch.object(MODULE, "_rule_exists", return_value=False),
            patch.object(
                MODULE,
                "_run",
                side_effect=(None, RuntimeError("tcp apply failed"), None, None),
            ) as run,
        ):
            with self.assertRaisesRegex(RuntimeError, "DNS fault apply failed"):
                MODULE.apply_dns_fault(
                    namespace="irlight-qa",
                    resolver="192.0.2.53",
                    rule_id="case-1",
                    duration_seconds=30,
                    confirm_disposable_namespace=True,
                )

        self.assertEqual(
            run.call_args_list,
            [
                call(plan.apply_argvs[0]),
                call(plan.apply_argvs[1]),
                call(plan.cleanup_argvs[1]),
                call(plan.cleanup_argvs[0]),
            ],
        )

    def test_cleanup_failure_is_not_reported_as_success(self) -> None:
        with (
            patch.object(MODULE, "_rule_exists", return_value=False),
            patch.object(
                MODULE,
                "_run",
                side_effect=(None, None, RuntimeError("tcp cleanup failed"), None),
            ),
            patch.object(MODULE.time, "sleep"),
        ):
            with self.assertRaisesRegex(RuntimeError, "DNS fault cleanup failed"):
                MODULE.apply_dns_fault(
                    namespace="irlight-qa",
                    resolver="192.0.2.53",
                    rule_id="case-1",
                    duration_seconds=30,
                    confirm_disposable_namespace=True,
                )

    def test_sleep_interrupt_still_cleans_both_rules_and_returns_130(self) -> None:
        plan = MODULE.build_dns_fault_plan(
            namespace="irlight-qa",
            resolver="192.0.2.53",
            rule_id="case-1",
            duration_seconds=30,
        )
        with (
            patch.object(MODULE, "_rule_exists", return_value=False),
            patch.object(MODULE, "_run") as run,
            patch.object(MODULE.time, "sleep", side_effect=KeyboardInterrupt),
        ):
            result = MODULE.apply_dns_fault(
                namespace="irlight-qa",
                resolver="192.0.2.53",
                rule_id="case-1",
                duration_seconds=30,
                confirm_disposable_namespace=True,
            )

        self.assertEqual(result, 130)
        self.assertEqual(
            run.call_args_list,
            [
                call(plan.apply_argvs[0]),
                call(plan.apply_argvs[1]),
                call(plan.cleanup_argvs[1]),
                call(plan.cleanup_argvs[0]),
            ],
        )

    def test_interrupt_during_second_apply_cleans_first_rule(self) -> None:
        plan = MODULE.build_dns_fault_plan(
            namespace="irlight-qa",
            resolver="192.0.2.53",
            rule_id="case-1",
            duration_seconds=30,
        )
        with (
            patch.object(MODULE, "_rule_exists", return_value=False),
            patch.object(
                MODULE,
                "_run",
                side_effect=(None, KeyboardInterrupt, None, None),
            ) as run,
        ):
            result = MODULE.apply_dns_fault(
                namespace="irlight-qa",
                resolver="192.0.2.53",
                rule_id="case-1",
                duration_seconds=30,
                confirm_disposable_namespace=True,
            )

        self.assertEqual(result, 130)
        self.assertEqual(
            run.call_args_list,
            [
                call(plan.apply_argvs[0]),
                call(plan.apply_argvs[1]),
                call(plan.cleanup_argvs[1]),
                call(plan.cleanup_argvs[0]),
            ],
        )

    def test_apply_refuses_preexisting_exact_rule_before_mutation(self) -> None:
        plan = MODULE.build_dns_fault_plan(
            namespace="irlight-qa",
            resolver="192.0.2.53",
            rule_id="case-1",
            duration_seconds=30,
        )
        with (
            patch.object(MODULE, "_rule_exists", side_effect=(False, True)) as exists,
            patch.object(MODULE, "_run") as run,
        ):
            with self.assertRaisesRegex(
                MODULE.DnsFaultPlanError, "already exists"
            ):
                MODULE.apply_dns_fault(
                    namespace="irlight-qa",
                    resolver="192.0.2.53",
                    rule_id="case-1",
                    duration_seconds=30,
                    confirm_disposable_namespace=True,
                )

        self.assertEqual(
            exists.call_args_list,
            [call(plan.check_argvs[0]), call(plan.check_argvs[1])],
        )
        run.assert_not_called()

    def test_clear_attempts_both_rules_even_if_first_cleanup_fails(self) -> None:
        plan = MODULE.build_dns_fault_plan(
            namespace="irlight-qa",
            resolver="192.0.2.53",
            rule_id="case-1",
            duration_seconds=None,
        )
        with patch.object(
            MODULE,
            "_run",
            side_effect=(RuntimeError("missing tcp rule"), None),
        ) as run:
            with self.assertRaisesRegex(RuntimeError, "DNS fault cleanup failed"):
                MODULE.clear_dns_fault(
                    namespace="irlight-qa",
                    resolver="192.0.2.53",
                    rule_id="case-1",
                    confirm_disposable_namespace=True,
                )

        self.assertEqual(
            run.call_args_list,
            [call(plan.cleanup_argvs[1]), call(plan.cleanup_argvs[0])],
        )


if __name__ == "__main__":
    unittest.main()
