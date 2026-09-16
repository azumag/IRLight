from __future__ import annotations

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

        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["protocols"], ["rtmp", "srt"])
        self.assertEqual(payload["profile_duration_seconds"], 30)
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

    def test_every_case_is_namespaced_and_read_only_generation_runs_no_commands(self) -> None:
        with patch.object(MATRIX.INJECTOR.subprocess, "run") as run:
            payload = MATRIX.build_matrix(namespace="irlight-qa", interface="eth0")

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

    def test_profile_duration_changes_only_loss_and_latency_cases(self) -> None:
        payload = MATRIX.build_matrix(
            namespace="irlight-qa",
            interface="eth0",
            protocols=("rtmp",),
            profile_duration_seconds=120,
        )

        regular = [case for case in payload["cases"] if not case["fault"]["disconnect"]]
        disconnects = [case for case in payload["cases"] if case["fault"]["disconnect"]]
        self.assertTrue(all(case["fault"]["duration_seconds"] == 120 for case in regular))
        self.assertEqual(
            {case["fault"]["duration_seconds"] for case in disconnects},
            {10, 30, 120, 600},
        )

    def test_programmatic_input_rejects_unsupported_protocol_and_duration(self) -> None:
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
