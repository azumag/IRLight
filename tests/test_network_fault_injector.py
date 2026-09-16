from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "network-fault-injector.py"
SPEC = importlib.util.spec_from_file_location("network_fault_injector", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
network_fault_injector = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = network_fault_injector
SPEC.loader.exec_module(network_fault_injector)


class NetworkFaultInjectorTests(unittest.TestCase):
    def test_loss_latency_and_jitter_plan_is_namespaced(self) -> None:
        plan = network_fault_injector.build_fault_plan(
            interface="eth0",
            namespace="irlight-qa",
            loss_percent=3,
            latency_ms=100,
            jitter_ms=20,
            disconnect=False,
            duration_seconds=30,
        )

        self.assertEqual(
            plan.apply_argv,
            (
                "ip",
                "netns",
                "exec",
                "irlight-qa",
                "tc",
                "qdisc",
                "replace",
                "dev",
                "eth0",
                "root",
                "netem",
                "loss",
                "3%",
                "delay",
                "100ms",
                "20ms",
            ),
        )
        self.assertEqual(
            plan.cleanup_argv,
            (
                "ip",
                "netns",
                "exec",
                "irlight-qa",
                "tc",
                "qdisc",
                "del",
                "dev",
                "eth0",
                "root",
            ),
        )
        self.assertEqual(plan.duration_seconds, 30)

    def test_correlated_burst_loss_plan_is_namespaced(self) -> None:
        plan = network_fault_injector.build_fault_plan(
            interface="eth0",
            namespace="irlight-qa",
            loss_percent=None,
            burst_loss_percent=5,
            burst_correlation_percent=75,
            latency_ms=None,
            jitter_ms=None,
            disconnect=False,
            duration_seconds=30,
        )

        self.assertEqual(
            plan.apply_argv[-4:],
            ("loss", "random", "5%", "75%"),
        )

    def test_burst_loss_reuses_loss_bounds_and_requires_bounded_correlation(self) -> None:
        with self.assertRaisesRegex(
            network_fault_injector.FaultPlanError, "burst_loss_percent"
        ):
            network_fault_injector.build_fault_plan(
                interface="eth0",
                namespace="irlight-qa",
                loss_percent=None,
                burst_loss_percent=2,
                burst_correlation_percent=50,
                latency_ms=None,
                jitter_ms=None,
                disconnect=False,
                duration_seconds=30,
            )

        for correlation in (True, 0, 100):
            with self.subTest(correlation=correlation):
                with self.assertRaisesRegex(
                    network_fault_injector.FaultPlanError,
                    "burst_correlation_percent",
                ):
                    network_fault_injector.build_fault_plan(
                        interface="eth0",
                        namespace="irlight-qa",
                        loss_percent=None,
                        burst_loss_percent=5,
                        burst_correlation_percent=correlation,
                        latency_ms=None,
                        jitter_ms=None,
                        disconnect=False,
                        duration_seconds=30,
                    )

    def test_burst_correlation_requires_burst_loss(self) -> None:
        with self.assertRaisesRegex(
            network_fault_injector.FaultPlanError, "requires burst loss"
        ):
            network_fault_injector.build_fault_plan(
                interface="eth0",
                namespace="irlight-qa",
                loss_percent=1,
                burst_correlation_percent=50,
                latency_ms=None,
                jitter_ms=None,
                disconnect=False,
                duration_seconds=30,
            )

    def test_burst_loss_cannot_combine_with_ordinary_loss(self) -> None:
        with self.assertRaisesRegex(
            network_fault_injector.FaultPlanError, "ordinary packet loss"
        ):
            network_fault_injector.build_fault_plan(
                interface="eth0",
                namespace="irlight-qa",
                loss_percent=1,
                burst_loss_percent=5,
                burst_correlation_percent=50,
                latency_ms=None,
                jitter_ms=None,
                disconnect=False,
                duration_seconds=30,
            )

    def test_bandwidth_plan_is_namespaced_and_bounded(self) -> None:
        plan = network_fault_injector.build_fault_plan(
            interface="eth0",
            namespace="irlight-qa",
            loss_percent=None,
            latency_ms=None,
            jitter_ms=None,
            disconnect=False,
            duration_seconds=30,
            bandwidth_kbit=2500,
        )

        self.assertEqual(
            plan.apply_argv,
            (
                "ip",
                "netns",
                "exec",
                "irlight-qa",
                "tc",
                "qdisc",
                "replace",
                "dev",
                "eth0",
                "root",
                "netem",
                "rate",
                "2500kbit",
            ),
        )

    def test_bandwidth_can_be_combined_with_loss_and_latency(self) -> None:
        plan = network_fault_injector.build_fault_plan(
            interface="eth0",
            namespace="irlight-qa",
            loss_percent=1,
            latency_ms=50,
            jitter_ms=10,
            disconnect=False,
            duration_seconds=10,
            bandwidth_kbit=4000,
        )

        self.assertEqual(
            plan.apply_argv[-7:],
            (
                "loss",
                "1%",
                "delay",
                "50ms",
                "10ms",
                "rate",
                "4000kbit",
            ),
        )

    def test_bandwidth_rejects_out_of_range_and_bool_values(self) -> None:
        for value in (
            True,
            network_fault_injector.BANDWIDTH_KBIT_MIN - 1,
            network_fault_injector.BANDWIDTH_KBIT_MAX + 1,
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    network_fault_injector.FaultPlanError, "bandwidth_kbit"
                ):
                    network_fault_injector.build_fault_plan(
                        interface="eth0",
                        namespace="irlight-qa",
                        loss_percent=None,
                        latency_ms=None,
                        jitter_ms=None,
                        disconnect=False,
                        duration_seconds=10,
                        bandwidth_kbit=value,
                    )

    def test_disconnect_is_a_complete_packet_blackhole(self) -> None:
        plan = network_fault_injector.build_fault_plan(
            interface="veth0",
            namespace="irlight-qa",
            loss_percent=None,
            latency_ms=None,
            jitter_ms=None,
            disconnect=True,
            duration_seconds=120,
        )
        self.assertEqual(plan.apply_argv[-2:], ("loss", "100%"))

    def test_jitter_requires_latency(self) -> None:
        with self.assertRaisesRegex(
            network_fault_injector.FaultPlanError, "jitter requires latency"
        ):
            network_fault_injector.build_fault_plan(
                interface="eth0",
                namespace="irlight-qa",
                loss_percent=1,
                latency_ms=None,
                jitter_ms=1,
                disconnect=False,
                duration_seconds=10,
            )

    def test_disconnect_cannot_hide_another_fault(self) -> None:
        with self.assertRaisesRegex(
            network_fault_injector.FaultPlanError, "cannot be combined"
        ):
            network_fault_injector.build_fault_plan(
                interface="eth0",
                namespace="irlight-qa",
                loss_percent=None,
                latency_ms=50,
                jitter_ms=None,
                disconnect=True,
                duration_seconds=10,
            )
        with self.assertRaisesRegex(
            network_fault_injector.FaultPlanError, "cannot be combined"
        ):
            network_fault_injector.build_fault_plan(
                interface="eth0",
                namespace="irlight-qa",
                loss_percent=None,
                latency_ms=None,
                jitter_ms=None,
                disconnect=True,
                duration_seconds=10,
                bandwidth_kbit=2500,
            )
        with self.assertRaisesRegex(
            network_fault_injector.FaultPlanError, "cannot be combined"
        ):
            network_fault_injector.build_fault_plan(
                interface="eth0",
                namespace="irlight-qa",
                loss_percent=None,
                burst_loss_percent=5,
                burst_correlation_percent=75,
                latency_ms=None,
                jitter_ms=None,
                disconnect=True,
                duration_seconds=10,
            )

    def test_loopback_requires_explicit_acknowledgement(self) -> None:
        with self.assertRaisesRegex(
            network_fault_injector.FaultPlanError, "loopback"
        ):
            network_fault_injector.build_fault_plan(
                interface="lo",
                namespace="irlight-qa",
                loss_percent=1,
                latency_ms=None,
                jitter_ms=None,
                disconnect=False,
                duration_seconds=10,
            )

    def test_interface_and_namespace_reject_option_like_names(self) -> None:
        with self.assertRaises(network_fault_injector.FaultPlanError):
            network_fault_injector.build_fault_plan(
                interface="--help",
                namespace="irlight-qa",
                loss_percent=1,
                latency_ms=None,
                jitter_ms=None,
                disconnect=False,
                duration_seconds=10,
            )
        with self.assertRaises(network_fault_injector.FaultPlanError):
            network_fault_injector.build_fault_plan(
                interface="eth0",
                namespace="--help",
                loss_percent=1,
                latency_ms=None,
                jitter_ms=None,
                disconnect=False,
                duration_seconds=10,
            )

    def test_apply_uses_namespace_and_cleans_up(self) -> None:
        with (
            mock.patch.object(network_fault_injector, "_run") as run,
            mock.patch.object(network_fault_injector.time, "sleep") as sleep,
        ):
            result = network_fault_injector.main(
                [
                    "apply",
                    "--interface",
                    "eth0",
                    "--namespace",
                    "irlight-qa",
                    "--loss",
                    "5",
                    "--duration",
                    "30",
                    "--confirm-disposable-namespace",
                ]
            )

        self.assertEqual(result, 0)
        sleep.assert_called_once_with(30)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(
            run.call_args_list[0].args[0][:4],
            ("ip", "netns", "exec", "irlight-qa"),
        )
        self.assertEqual(
            run.call_args_list[1].args[0],
            (
                "ip",
                "netns",
                "exec",
                "irlight-qa",
                "tc",
                "qdisc",
                "del",
                "dev",
                "eth0",
                "root",
            ),
        )

    def test_burst_loss_cli_requires_correlation_before_execution(self) -> None:
        with mock.patch.object(network_fault_injector, "_run") as run:
            result = network_fault_injector.main(
                [
                    "apply",
                    "--interface",
                    "eth0",
                    "--namespace",
                    "irlight-qa",
                    "--burst-loss",
                    "5",
                    "--duration",
                    "30",
                    "--confirm-disposable-namespace",
                ]
            )
        self.assertEqual(result, 2)
        run.assert_not_called()

    def test_apply_failure_still_attempts_cleanup(self) -> None:
        with (
            mock.patch.object(
                network_fault_injector,
                "_run",
                side_effect=[RuntimeError("apply failed"), None],
            ) as run,
            mock.patch.object(network_fault_injector.time, "sleep") as sleep,
        ):
            result = network_fault_injector.main(
                [
                    "apply",
                    "--interface",
                    "eth0",
                    "--namespace",
                    "irlight-qa",
                    "--bandwidth-kbit",
                    "2500",
                    "--duration",
                    "30",
                    "--confirm-disposable-namespace",
                ]
            )

        self.assertEqual(result, 2)
        self.assertEqual(run.call_count, 2)
        sleep.assert_not_called()

    def test_apply_and_cleanup_failure_reports_failure(self) -> None:
        with (
            mock.patch.object(
                network_fault_injector,
                "_run",
                side_effect=[RuntimeError("apply failed"), RuntimeError("cleanup failed")],
            ) as run,
            mock.patch.object(network_fault_injector.time, "sleep") as sleep,
        ):
            result = network_fault_injector.main(
                [
                    "apply",
                    "--interface",
                    "eth0",
                    "--namespace",
                    "irlight-qa",
                    "--loss",
                    "1",
                    "--duration",
                    "10",
                    "--confirm-disposable-namespace",
                ]
            )

        self.assertEqual(result, 2)
        self.assertEqual(run.call_count, 2)
        sleep.assert_not_called()

    def test_keyboard_interrupt_still_cleans_up(self) -> None:
        with (
            mock.patch.object(network_fault_injector, "_run") as run,
            mock.patch.object(
                network_fault_injector.time, "sleep", side_effect=KeyboardInterrupt
            ),
        ):
            result = network_fault_injector.main(
                [
                    "apply",
                    "--interface",
                    "eth0",
                    "--namespace",
                    "irlight-qa",
                    "--disconnect",
                    "--duration",
                    "10",
                    "--confirm-disposable-namespace",
                ]
            )

        self.assertEqual(result, 130)
        self.assertEqual(run.call_count, 2)

    def test_clear_never_targets_host_namespace(self) -> None:
        with mock.patch.object(network_fault_injector, "_run") as run:
            result = network_fault_injector.main(
                [
                    "clear",
                    "--interface",
                    "eth0",
                    "--namespace",
                    "irlight-qa",
                    "--confirm-disposable-namespace",
                ]
            )

        self.assertEqual(result, 0)
        run.assert_called_once_with(
            (
                "ip",
                "netns",
                "exec",
                "irlight-qa",
                "tc",
                "qdisc",
                "del",
                "dev",
                "eth0",
                "root",
            )
        )

    def test_plan_parser_requires_namespace(self) -> None:
        parser = network_fault_injector._parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["plan", "--interface", "eth0", "--loss", "1"])

    def test_apply_parser_requires_namespace_duration_and_acknowledgement(self) -> None:
        parser = network_fault_injector._parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["apply", "--interface", "eth0", "--loss", "1"])


if __name__ == "__main__":
    unittest.main()
