from __future__ import annotations

import argparse
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "network-fault-matrix.py"


def _load_matrix_module():
    spec = importlib.util.spec_from_file_location("irlight_network_fault_matrix_test", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("network fault matrix module cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MATRIX = _load_matrix_module()


class NetworkFaultMatrixTest(unittest.TestCase):
    def test_default_matrix_covers_rtmp_and_srt_baseline(self) -> None:
        payload = MATRIX.build_matrix(namespace="irlight-qa", interface="eth0")

        self.assertEqual(payload["schema_version"], 4)
        self.assertEqual(payload["protocols"], ["rtmp", "srt"])
        self.assertEqual(payload["profile_duration_seconds"], 30)
        self.assertEqual(payload["bandwidth_kbits"], [])
        self.assertEqual(payload["jitter_profiles"], [])
        self.assertEqual(payload["burst_loss_profiles"], [])
        self.assertEqual(payload["case_count"], 24)

        cases = payload["cases"]
        case_ids = [case["id"] for case in cases]
        self.assertEqual(len(case_ids), len(set(case_ids)))

        for protocol in ("rtmp", "srt"):
            protocol_cases = [case for case in cases if case["protocol"] == protocol]
            self.assertEqual(len(protocol_cases), 12)

            loss_cases = [
                case for case in protocol_cases if case["fault"]["loss_percent"] is not None
            ]
            self.assertEqual(
                {case["fault"]["loss_percent"] for case in loss_cases},
                {1, 3, 5, 10},
            )
            self.assertTrue(
                all(case["fault"]["duration_seconds"] == 30 for case in loss_cases)
            )

            latency_cases = [
                case for case in protocol_cases if case["fault"]["latency_ms"] is not None
            ]
            self.assertEqual(
                {case["fault"]["latency_ms"] for case in latency_cases},
                {50, 100, 300, 1000},
            )
            self.assertTrue(
                all(case["fault"]["duration_seconds"] == 30 for case in latency_cases)
            )

            disconnect_cases = [
                case for case in protocol_cases if case["fault"]["disconnect"]
            ]
            self.assertEqual(
                {case["fault"]["duration_seconds"] for case in disconnect_cases},
                {10, 30, 120, 600},
            )
            self.assertTrue(
                all(case["fault"]["bandwidth_kbit"] is None for case in protocol_cases)
            )
            self.assertTrue(
                all(case["fault"]["jitter_ms"] is None for case in protocol_cases)
            )
            self.assertTrue(
                all(case["fault"]["burst_loss_percent"] is None for case in protocol_cases)
            )
            self.assertTrue(
                all(
                    case["fault"]["burst_correlation_percent"] is None
                    for case in protocol_cases
                )
            )

    def test_explicit_burst_loss_cases_are_additive_and_deduplicated(self) -> None:
        payload = MATRIX.build_matrix(
            namespace="irlight-qa",
            interface="eth0",
            protocols=("rtmp",),
            burst_loss_profiles=((5, 75), (5, 75), (10, 50)),
        )

        self.assertEqual(
            payload["burst_loss_profiles"],
            [
                {"loss_percent": 5, "correlation_percent": 75},
                {"loss_percent": 10, "correlation_percent": 50},
            ],
        )
        self.assertEqual(payload["case_count"], 14)
        burst_cases = [
            case
            for case in payload["cases"]
            if case["fault"]["burst_loss_percent"] is not None
        ]
        self.assertEqual(
            [case["id"] for case in burst_cases],
            [
                "rtmp-burst-loss-5pct-corr-75pct-30s",
                "rtmp-burst-loss-10pct-corr-50pct-30s",
            ],
        )
        self.assertEqual(
            [
                (
                    case["fault"]["burst_loss_percent"],
                    case["fault"]["burst_correlation_percent"],
                )
                for case in burst_cases
            ],
            [(5, 75), (10, 50)],
        )
        self.assertEqual(
            burst_cases[0]["plan"]["apply_argv"][-4:],
            ["loss", "random", "5%", "75%"],
        )

    def test_explicit_jitter_cases_are_additive_and_deduplicated(self) -> None:
        payload = MATRIX.build_matrix(
            namespace="irlight-qa",
            interface="eth0",
            protocols=("rtmp",),
            jitter_profiles=((100, 20), (100, 20), (300, 50)),
        )

        self.assertEqual(
            payload["jitter_profiles"],
            [
                {"latency_ms": 100, "jitter_ms": 20},
                {"latency_ms": 300, "jitter_ms": 50},
            ],
        )
        self.assertEqual(payload["case_count"], 14)
        jitter_cases = [
            case for case in payload["cases"] if case["fault"]["jitter_ms"] is not None
        ]
        self.assertEqual(
            [case["id"] for case in jitter_cases],
            [
                "rtmp-jitter-100ms-20ms-30s",
                "rtmp-jitter-300ms-50ms-30s",
            ],
        )
        self.assertEqual(
            [
                (case["fault"]["latency_ms"], case["fault"]["jitter_ms"])
                for case in jitter_cases
            ],
            [(100, 20), (300, 50)],
        )
        self.assertIn("100ms", jitter_cases[0]["plan"]["apply_argv"])
        self.assertIn("20ms", jitter_cases[0]["plan"]["apply_argv"])

    def test_explicit_bandwidth_cases_are_additive_and_deduplicated(self) -> None:
        payload = MATRIX.build_matrix(
            namespace="irlight-qa",
            interface="eth0",
            protocols=("rtmp",),
            bandwidth_kbits=(2500, 2500, 4000),
        )

        self.assertEqual(payload["bandwidth_kbits"], [2500, 4000])
        self.assertEqual(payload["case_count"], 14)
        bandwidth_cases = [
            case for case in payload["cases"] if case["fault"]["bandwidth_kbit"] is not None
        ]
        self.assertEqual(
            [case["id"] for case in bandwidth_cases],
            ["rtmp-bandwidth-2500kbit-30s", "rtmp-bandwidth-4000kbit-30s"],
        )
        self.assertEqual(
            [case["fault"]["bandwidth_kbit"] for case in bandwidth_cases],
            [2500, 4000],
        )
        self.assertTrue(
            all(case["fault"]["duration_seconds"] == 30 for case in bandwidth_cases)
        )
        self.assertIn(
            "2500kbit",
            bandwidth_cases[0]["plan"]["apply_argv"],
        )
        self.assertIn(
            "4000kbit",
            bandwidth_cases[1]["plan"]["apply_argv"],
        )

    def test_every_case_is_namespaced_and_read_only_generation_runs_no_commands(self) -> None:
        with patch.object(MATRIX.INJECTOR.subprocess, "run") as run:
            payload = MATRIX.build_matrix(
                namespace="irlight-qa",
                interface="eth0",
                bandwidth_kbits=(2500,),
                jitter_profiles=((100, 20),),
                burst_loss_profiles=((5, 75),),
            )

        run.assert_not_called()
        for case in payload["cases"]:
            plan = case["plan"]
            self.assertEqual(
                plan["apply_argv"][:4], ["ip", "netns", "exec", "irlight-qa"]
            )
            self.assertEqual(
                plan["cleanup_argv"][:4], ["ip", "netns", "exec", "irlight-qa"]
            )

    def test_protocol_filter_is_deterministic_and_deduplicated(self) -> None:
        payload = MATRIX.build_matrix(
            namespace="irlight-qa",
            interface="eth0",
            protocols=("srt", "srt"),
        )

        self.assertEqual(payload["protocols"], ["srt"])
        self.assertEqual(payload["case_count"], 12)
        self.assertTrue(all(case["protocol"] == "srt" for case in payload["cases"]))

    def test_profile_duration_changes_loss_burst_latency_jitter_and_bandwidth_cases(self) -> None:
        payload = MATRIX.build_matrix(
            namespace="irlight-qa",
            interface="eth0",
            protocols=("rtmp",),
            profile_duration_seconds=120,
            bandwidth_kbits=(2500,),
            jitter_profiles=((100, 20),),
            burst_loss_profiles=((5, 75),),
        )

        regular = [case for case in payload["cases"] if not case["fault"]["disconnect"]]
        disconnects = [case for case in payload["cases"] if case["fault"]["disconnect"]]
        self.assertTrue(all(case["fault"]["duration_seconds"] == 120 for case in regular))
        self.assertIn("rtmp-burst-loss-5pct-corr-75pct-120s", [case["id"] for case in regular])
        self.assertIn("rtmp-jitter-100ms-20ms-120s", [case["id"] for case in regular])
        self.assertIn("rtmp-bandwidth-2500kbit-120s", [case["id"] for case in regular])
        self.assertEqual(
            {case["fault"]["duration_seconds"] for case in disconnects},
            {10, 30, 120, 600},
        )

    def test_programmatic_input_rejects_unsupported_protocol_duration_bandwidth_jitter_and_burst(self) -> None:
        with self.assertRaises(MATRIX.MatrixError):
            MATRIX.build_matrix(
                namespace="irlight-qa",
                interface="eth0",
                protocols=("udp",),
            )
        with self.assertRaises(MATRIX.MatrixError):
            MATRIX.build_matrix(
                namespace="irlight-qa",
                interface="eth0",
                profile_duration_seconds=999,
            )
        for bandwidth_kbits in ((63,), (100001,), (True,)):
            with self.assertRaises(MATRIX.MatrixError):
                MATRIX.build_matrix(
                    namespace="irlight-qa",
                    interface="eth0",
                    bandwidth_kbits=bandwidth_kbits,
                )
        for jitter_profiles in (((75, 10),), ((100, 0),), ((100, 101),), ((100, True),)):
            with self.assertRaises(MATRIX.MatrixError):
                MATRIX.build_matrix(
                    namespace="irlight-qa",
                    interface="eth0",
                    jitter_profiles=jitter_profiles,
                )
        for burst_loss_profiles in (((2, 50),), ((5, 0),), ((5, 100),), ((5, True),)):
            with self.assertRaises(MATRIX.MatrixError):
                MATRIX.build_matrix(
                    namespace="irlight-qa",
                    interface="eth0",
                    burst_loss_profiles=burst_loss_profiles,
                )

    def test_jitter_profile_cli_parser_is_fail_closed(self) -> None:
        self.assertEqual(MATRIX.parse_jitter_profile("100:20"), (100, 20))
        for value in ("100", "100:20:3", "abc:20", "75:10", "100:101"):
            with self.assertRaises(argparse.ArgumentTypeError):
                MATRIX.parse_jitter_profile(value)

    def test_burst_loss_profile_cli_parser_is_fail_closed(self) -> None:
        self.assertEqual(MATRIX.parse_burst_loss_profile("5:75"), (5, 75))
        for value in ("5", "5:75:1", "abc:75", "2:50", "5:0", "5:100"):
            with self.assertRaises(argparse.ArgumentTypeError):
                MATRIX.parse_burst_loss_profile(value)

    def test_namespace_and_interface_safety_validation_is_reused(self) -> None:
        with self.assertRaises(MATRIX.INJECTOR.FaultPlanError):
            MATRIX.build_matrix(namespace="-host", interface="eth0")
        with self.assertRaises(MATRIX.INJECTOR.FaultPlanError):
            MATRIX.build_matrix(namespace="irlight-qa", interface="lo")

        payload = MATRIX.build_matrix(
            namespace="irlight-qa",
            interface="lo",
            protocols=("rtmp",),
            allow_loopback=True,
        )
        self.assertEqual(payload["case_count"], 12)


if __name__ == "__main__":
    unittest.main()
