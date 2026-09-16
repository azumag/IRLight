from __future__ import annotations

import importlib.util
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import call, patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "network-route-fault-injector.py"
SPEC = importlib.util.spec_from_file_location("network_route_fault_injector", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class NetworkRouteFaultInjectorTest(unittest.TestCase):
    def test_ipv4_plan_adds_tagged_blackhole_host_route_inside_namespace(self) -> None:
        plan = MODULE.build_route_fault_plan(
            namespace="irlight-qa",
            destination="192.0.2.25",
            duration_seconds=30,
        )

        self.assertEqual(plan.destination, "192.0.2.25")
        self.assertEqual(plan.prefix, "192.0.2.25/32")
        self.assertEqual(plan.duration_seconds, 30)
        self.assertEqual(
            plan.check_argv,
            (
                "ip",
                "netns",
                "exec",
                "irlight-qa",
                "ip",
                "-json",
                "route",
                "show",
                "table",
                "main",
                "exact",
                "192.0.2.25/32",
            ),
        )
        self.assertEqual(
            plan.apply_argv,
            (
                "ip",
                "netns",
                "exec",
                "irlight-qa",
                "ip",
                "route",
                "add",
                "blackhole",
                "192.0.2.25/32",
                "table",
                "main",
                "proto",
                "99",
                "metric",
                "42760",
            ),
        )
        self.assertEqual(
            plan.cleanup_argv,
            (
                "ip",
                "netns",
                "exec",
                "irlight-qa",
                "ip",
                "route",
                "del",
                "blackhole",
                "192.0.2.25/32",
                "table",
                "main",
                "proto",
                "99",
                "metric",
                "42760",
            ),
        )

    def test_ipv6_plan_uses_ipv6_route_family_and_normalizes_address(self) -> None:
        plan = MODULE.build_route_fault_plan(
            namespace="irlight-qa",
            destination="2001:0db8:0000:0000:0000:0000:0000:0025",
            duration_seconds=10,
        )

        self.assertEqual(plan.destination, "2001:db8::25")
        self.assertEqual(plan.prefix, "2001:db8::25/128")
        self.assertEqual(plan.apply_argv[5], "-6")
        self.assertIn("2001:db8::25/128", plan.apply_argv)

    def test_hostname_unspecified_and_multicast_destinations_are_rejected(self) -> None:
        for destination in ("route.invalid", "0.0.0.0", "224.0.0.1", "::", "ff02::1"):
            with self.subTest(destination=destination):
                with self.assertRaises(MODULE.RouteFaultPlanError):
                    MODULE.build_route_fault_plan(
                        namespace="irlight-qa",
                        destination=destination,
                        duration_seconds=30,
                    )

    def test_loopback_requires_explicit_opt_in(self) -> None:
        with self.assertRaisesRegex(
            MODULE.RouteFaultPlanError, "loopback destination requires"
        ):
            MODULE.build_route_fault_plan(
                namespace="irlight-qa",
                destination="127.0.0.1",
                duration_seconds=30,
            )

        plan = MODULE.build_route_fault_plan(
            namespace="irlight-qa",
            destination="127.0.0.1",
            duration_seconds=30,
            allow_loopback=True,
        )
        self.assertEqual(plan.prefix, "127.0.0.1/32")

    def test_namespace_and_duration_are_strict(self) -> None:
        invalid_cases = (
            {
                "namespace": "../host",
                "destination": "192.0.2.25",
                "duration_seconds": 30,
            },
            {
                "namespace": "irlight-qa",
                "destination": "192.0.2.25",
                "duration_seconds": True,
            },
            {
                "namespace": "irlight-qa",
                "destination": "192.0.2.25",
                "duration_seconds": 5,
            },
        )
        for kwargs in invalid_cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(MODULE.RouteFaultPlanError):
                    MODULE.build_route_fault_plan(**kwargs)

    def test_plan_command_is_read_only(self) -> None:
        output = io.StringIO()
        with patch.object(MODULE.subprocess, "run") as run, redirect_stdout(output):
            result = MODULE.main(
                [
                    "plan",
                    "--namespace",
                    "irlight-qa",
                    "--destination",
                    "192.0.2.25",
                    "--duration",
                    "30",
                    "--json",
                ]
            )

        self.assertEqual(result, 0)
        run.assert_not_called()
        self.assertIn('"prefix": "192.0.2.25/32"', output.getvalue())
        self.assertIn('"duration_seconds": 30', output.getvalue())

    def test_programmatic_apply_requires_disposable_namespace_acknowledgement(self) -> None:
        with patch.object(MODULE.subprocess, "run") as run:
            with self.assertRaisesRegex(
                MODULE.RouteFaultPlanError, "acknowledgement is required"
            ):
                MODULE.apply_route_fault(
                    namespace="irlight-qa",
                    destination="192.0.2.25",
                    duration_seconds=30,
                    confirm_disposable_namespace=False,
                )

        run.assert_not_called()

    def test_preexisting_exact_route_is_refused_before_mutation(self) -> None:
        with (
            patch.object(MODULE, "_exact_route_exists", return_value=True) as exists,
            patch.object(MODULE, "_run") as run,
        ):
            with self.assertRaisesRegex(
                MODULE.RouteFaultPlanError, "exact destination route already exists"
            ):
                MODULE.apply_route_fault(
                    namespace="irlight-qa",
                    destination="192.0.2.25",
                    duration_seconds=30,
                    confirm_disposable_namespace=True,
                )

        exists.assert_called_once()
        run.assert_not_called()

    def test_invalid_preflight_payload_does_not_mutate_routes(self) -> None:
        with (
            patch.object(MODULE, "_run_capture", return_value=(0, "{not-json")) as capture,
            patch.object(MODULE, "_run") as run,
        ):
            with self.assertRaisesRegex(RuntimeError, "preflight returned invalid JSON"):
                MODULE.apply_route_fault(
                    namespace="irlight-qa",
                    destination="192.0.2.25",
                    duration_seconds=30,
                    confirm_disposable_namespace=True,
                )

        capture.assert_called_once()
        run.assert_not_called()

    def test_apply_adds_route_then_cleans_up(self) -> None:
        plan = MODULE.build_route_fault_plan(
            namespace="irlight-qa",
            destination="192.0.2.25",
            duration_seconds=30,
        )
        with (
            patch.object(MODULE, "_exact_route_exists", return_value=False),
            patch.object(MODULE, "_run") as run,
            patch.object(MODULE.time, "sleep") as sleep,
        ):
            result = MODULE.apply_route_fault(
                namespace="irlight-qa",
                destination="192.0.2.25",
                duration_seconds=30,
                confirm_disposable_namespace=True,
            )

        self.assertEqual(result, 0)
        self.assertEqual(
            run.call_args_list,
            [call(plan.apply_argv), call(plan.cleanup_argv)],
        )
        sleep.assert_called_once_with(30)

    def test_apply_failure_still_attempts_exact_cleanup(self) -> None:
        plan = MODULE.build_route_fault_plan(
            namespace="irlight-qa",
            destination="192.0.2.25",
            duration_seconds=30,
        )
        with (
            patch.object(MODULE, "_exact_route_exists", return_value=False),
            patch.object(
                MODULE,
                "_run",
                side_effect=(RuntimeError("apply timed out"), None),
            ) as run,
        ):
            with self.assertRaisesRegex(RuntimeError, "route fault apply failed"):
                MODULE.apply_route_fault(
                    namespace="irlight-qa",
                    destination="192.0.2.25",
                    duration_seconds=30,
                    confirm_disposable_namespace=True,
                )

        self.assertEqual(
            run.call_args_list,
            [call(plan.apply_argv), call(plan.cleanup_argv)],
        )

    def test_cleanup_failure_is_not_reported_as_success(self) -> None:
        with (
            patch.object(MODULE, "_exact_route_exists", return_value=False),
            patch.object(
                MODULE,
                "_run",
                side_effect=(None, RuntimeError("cleanup failed")),
            ),
            patch.object(MODULE.time, "sleep"),
        ):
            with self.assertRaisesRegex(RuntimeError, "route fault cleanup failed"):
                MODULE.apply_route_fault(
                    namespace="irlight-qa",
                    destination="192.0.2.25",
                    duration_seconds=30,
                    confirm_disposable_namespace=True,
                )

    def test_sleep_interrupt_cleans_route_and_returns_130(self) -> None:
        plan = MODULE.build_route_fault_plan(
            namespace="irlight-qa",
            destination="192.0.2.25",
            duration_seconds=30,
        )
        with (
            patch.object(MODULE, "_exact_route_exists", return_value=False),
            patch.object(MODULE, "_run") as run,
            patch.object(MODULE.time, "sleep", side_effect=KeyboardInterrupt),
        ):
            result = MODULE.apply_route_fault(
                namespace="irlight-qa",
                destination="192.0.2.25",
                duration_seconds=30,
                confirm_disposable_namespace=True,
            )

        self.assertEqual(result, 130)
        self.assertEqual(
            run.call_args_list,
            [call(plan.apply_argv), call(plan.cleanup_argv)],
        )

    def test_interrupt_during_apply_still_attempts_cleanup(self) -> None:
        plan = MODULE.build_route_fault_plan(
            namespace="irlight-qa",
            destination="192.0.2.25",
            duration_seconds=30,
        )
        with (
            patch.object(MODULE, "_exact_route_exists", return_value=False),
            patch.object(
                MODULE,
                "_run",
                side_effect=(KeyboardInterrupt, None),
            ) as run,
        ):
            result = MODULE.apply_route_fault(
                namespace="irlight-qa",
                destination="192.0.2.25",
                duration_seconds=30,
                confirm_disposable_namespace=True,
            )

        self.assertEqual(result, 130)
        self.assertEqual(
            run.call_args_list,
            [call(plan.apply_argv), call(plan.cleanup_argv)],
        )

    def test_clear_requires_acknowledgement_and_deletes_exact_tagged_route(self) -> None:
        plan = MODULE.build_route_fault_plan(
            namespace="irlight-qa",
            destination="192.0.2.25",
            duration_seconds=None,
        )
        with patch.object(MODULE, "_run") as run:
            with self.assertRaises(MODULE.RouteFaultPlanError):
                MODULE.clear_route_fault(
                    namespace="irlight-qa",
                    destination="192.0.2.25",
                    confirm_disposable_namespace=False,
                )
            run.assert_not_called()

            MODULE.clear_route_fault(
                namespace="irlight-qa",
                destination="192.0.2.25",
                confirm_disposable_namespace=True,
            )

        run.assert_called_once_with(plan.cleanup_argv)


if __name__ == "__main__":
    unittest.main()
