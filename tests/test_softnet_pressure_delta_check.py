from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-softnet-pressure-delta.sh"


def softnet_line(processed: int, dropped: int, squeeze: int, *extra: int) -> str:
    fields = [processed, dropped, squeeze, *extra]
    return " ".join(f"{value:08x}" for value in fields) + "\n"


class SoftnetPressureDeltaCheckTests(unittest.TestCase):
    def run_check(
        self,
        current: str | None,
        baseline: str | None,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="irlight-softnet-pressure-") as temporary:
            root = Path(temporary)
            current_path = root / "current.softnet"
            baseline_path = root / "baseline.softnet"
            if current is not None:
                current_path.write_text(current, encoding="ascii")
            if baseline is not None:
                baseline_path.write_text(baseline, encoding="ascii")

            env = dict(os.environ)
            env.pop("IRLIGHT_SOFTNET_STAT_PATH", None)
            env.pop("IRLIGHT_SOFTNET_STAT_BASELINE_PATH", None)
            return subprocess.run(
                ["bash", str(SCRIPT), str(current_path), str(baseline_path)],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )

    def test_multi_cpu_no_delta_is_ok(self) -> None:
        snapshot = softnet_line(100, 2, 3, 4) + softnet_line(200, 5, 7, 8)
        result = self.run_check(snapshot, snapshot)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            "IRLIGHT_SOFTNET_PRESSURE status=OK reason=none dropped_delta=0 time_squeeze_delta=0\n",
        )

    def test_multi_cpu_deltas_are_aggregated(self) -> None:
        baseline = softnet_line(100, 2, 3) + softnet_line(200, 5, 7)
        current = softnet_line(120, 4, 4) + softnet_line(240, 8, 11)
        result = self.run_check(current, baseline)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(
            result.stdout,
            "IRLIGHT_SOFTNET_PRESSURE status=WARNING reason=softnet_pressure_activity dropped_delta=5 time_squeeze_delta=5\n",
        )

    def test_time_squeeze_delta_is_warning(self) -> None:
        baseline = softnet_line(100, 0, 2)
        current = softnet_line(150, 0, 9)
        result = self.run_check(current, baseline)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("dropped_delta=0 time_squeeze_delta=7", result.stdout)

    def test_per_cpu_counter_reset_is_unknown_even_when_aggregate_grows(self) -> None:
        baseline = softnet_line(100, 10, 12) + softnet_line(100, 0, 0)
        current = softnet_line(90, 9, 13) + softnet_line(500, 50, 50)
        result = self.run_check(current, baseline)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(
            result.stdout,
            "IRLIGHT_SOFTNET_PRESSURE status=UNKNOWN reason=counter_reset\n",
        )

    def test_cpu_record_count_change_is_unknown(self) -> None:
        baseline = softnet_line(100, 1, 2)
        current = baseline + softnet_line(10, 0, 0)
        result = self.run_check(current, baseline)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(
            result.stdout,
            "IRLIGHT_SOFTNET_PRESSURE status=UNKNOWN reason=cpu_topology_changed\n",
        )

    def test_missing_inputs_are_unknown_without_path_echo(self) -> None:
        result = self.run_check(None, softnet_line(1, 0, 0))
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout,
            "IRLIGHT_SOFTNET_PRESSURE status=UNKNOWN reason=current_softnet_unavailable\n",
        )
        self.assertNotIn("irlight-softnet-pressure-", result.stdout + result.stderr)

        result = self.run_check(softnet_line(1, 0, 0), None)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout,
            "IRLIGHT_SOFTNET_PRESSURE status=UNKNOWN reason=baseline_softnet_unavailable\n",
        )
        self.assertNotIn("irlight-softnet-pressure-", result.stdout + result.stderr)

    def test_missing_baseline_configuration_is_unknown(self) -> None:
        env = dict(os.environ)
        env.pop("IRLIGHT_SOFTNET_STAT_BASELINE_PATH", None)
        with tempfile.TemporaryDirectory(prefix="irlight-softnet-pressure-") as temporary:
            current_path = Path(temporary) / "current.softnet"
            current_path.write_text(softnet_line(1, 0, 0), encoding="ascii")
            result = subprocess.run(
                ["bash", str(SCRIPT), str(current_path)],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout,
            "IRLIGHT_SOFTNET_PRESSURE status=UNKNOWN reason=baseline_softnet_unavailable\n",
        )

    def test_environment_paths_are_supported(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-softnet-pressure-") as temporary:
            root = Path(temporary)
            current_path = root / "current.softnet"
            baseline_path = root / "baseline.softnet"
            baseline_path.write_text(softnet_line(1, 0, 0), encoding="ascii")
            current_path.write_text(softnet_line(2, 1, 0), encoding="ascii")
            env = dict(os.environ)
            env["IRLIGHT_SOFTNET_STAT_PATH"] = str(current_path)
            env["IRLIGHT_SOFTNET_STAT_BASELINE_PATH"] = str(baseline_path)
            result = subprocess.run(
                ["bash", str(SCRIPT)],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("dropped_delta=1", result.stdout)

    def test_malformed_target_fields_are_unknown(self) -> None:
        baseline = softnet_line(1, 0, 0)
        for current in (
            "00000001 00000000\n",
            "00000001 nothex00 00000000\n",
            "00000001 0000000 00000000\n",
            "\n",
        ):
            with self.subTest(current=current):
                result = self.run_check(current, baseline)
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertEqual(
                    result.stdout,
                    "IRLIGHT_SOFTNET_PRESSURE status=UNKNOWN reason=invalid_softnet_record\n",
                )

    def test_future_extra_fields_are_accepted(self) -> None:
        baseline = softnet_line(1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13)
        current = softnet_line(2, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 99)
        result = self.run_check(current, baseline)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("status=OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
