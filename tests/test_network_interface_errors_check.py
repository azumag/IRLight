from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-network-interface-errors.sh"
COUNTERS = ("rx_errors", "tx_errors", "rx_dropped", "tx_dropped")


def values(**overrides: int | str) -> dict[str, str]:
    result = {key: "0" for key in COUNTERS}
    result.update({key: str(value) for key, value in overrides.items()})
    return result


class NetworkInterfaceErrorsCheckTest(unittest.TestCase):
    def _run(
        self,
        *,
        current: dict[str, str] | None = None,
        baseline: dict[str, str] | None = None,
        missing_current: str | None = None,
        missing_baseline: str | None = None,
        use_environment: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="irlight-network-interface-errors-") as temporary:
            root = Path(temporary)
            current_dir = root / "current"
            baseline_dir = root / "baseline"
            current_dir.mkdir()
            baseline_dir.mkdir()

            current_values = values() if current is None else current
            baseline_values = values() if baseline is None else baseline
            for key in COUNTERS:
                if key != missing_current:
                    (current_dir / key).write_text(current_values[key] + "\n", encoding="utf-8")
                if key != missing_baseline:
                    (baseline_dir / key).write_text(baseline_values[key] + "\n", encoding="utf-8")

            env = os.environ.copy()
            args = ["bash", str(SCRIPT)]
            if use_environment:
                env["IRLIGHT_NETWORK_STATS_DIR"] = str(current_dir)
                env["IRLIGHT_NETWORK_STATS_BASELINE_DIR"] = str(baseline_dir)
            else:
                args.extend([str(current_dir), str(baseline_dir)])

            return subprocess.run(
                args,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

    def test_no_new_events_is_ok(self) -> None:
        baseline = values(rx_errors=2, tx_errors=3, rx_dropped=4, tx_dropped=5)
        result = self._run(current=baseline, baseline=baseline)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_NETWORK_INTERFACE_ERRORS status=OK reason=none rx_errors_delta=0 tx_errors_delta=0 rx_dropped_delta=0 tx_dropped_delta=0",
        )

    def test_error_delta_is_critical(self) -> None:
        for key in ("rx_errors", "tx_errors"):
            with self.subTest(key=key):
                result = self._run(current=values(**{key: 1}))
                self.assertEqual(result.returncode, 2)
                self.assertIn("status=CRITICAL", result.stdout)
                self.assertIn("reason=interface_error_activity", result.stdout)
                self.assertIn(f"{key}_delta=1", result.stdout)

    def test_drop_delta_is_warning(self) -> None:
        for key in ("rx_dropped", "tx_dropped"):
            with self.subTest(key=key):
                result = self._run(current=values(**{key: 2}))
                self.assertEqual(result.returncode, 1)
                self.assertIn("status=WARNING", result.stdout)
                self.assertIn("reason=interface_drop_activity", result.stdout)
                self.assertIn(f"{key}_delta=2", result.stdout)

    def test_confirmed_error_dominates_drop_warning(self) -> None:
        result = self._run(current=values(rx_errors=1, tx_dropped=20))
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("reason=interface_error_activity", result.stdout)
        self.assertIn("tx_dropped_delta=20", result.stdout)

    def test_counter_reset_is_unknown(self) -> None:
        result = self._run(current=values(rx_dropped=4), baseline=values(rx_dropped=5))
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_NETWORK_INTERFACE_ERRORS status=UNKNOWN reason=counter_reset",
        )

    def test_missing_current_counter_is_unknown(self) -> None:
        result = self._run(missing_current="tx_errors")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_NETWORK_INTERFACE_ERRORS status=UNKNOWN reason=current_stats_unavailable",
        )

    def test_missing_baseline_counter_is_unknown(self) -> None:
        result = self._run(missing_baseline="rx_dropped")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_NETWORK_INTERFACE_ERRORS status=UNKNOWN reason=baseline_stats_unavailable",
        )

    def test_malformed_counter_is_unknown(self) -> None:
        malformed = ("", "-1", "nope", "1 extra", "1\n2", "9223372036854775808")
        for value in malformed:
            with self.subTest(value=value):
                current = values(rx_errors=value)
                result = self._run(current=current)
                self.assertEqual(result.returncode, 3)
                self.assertEqual(
                    result.stdout.strip(),
                    "IRLIGHT_NETWORK_INTERFACE_ERRORS status=UNKNOWN reason=invalid_stats_record",
                )

    def test_signed_64_bit_maximum_is_supported(self) -> None:
        result = self._run(
            current=values(tx_errors=9223372036854775807),
            baseline=values(tx_errors=9223372036854775806),
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("tx_errors_delta=1", result.stdout)

    def test_environment_paths_are_supported(self) -> None:
        result = self._run(current=values(rx_dropped=1), use_environment=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("rx_dropped_delta=1", result.stdout)


if __name__ == "__main__":
    unittest.main()
