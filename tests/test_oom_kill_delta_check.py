from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-oom-kill-delta.sh"


class OomKillDeltaCheckTests(unittest.TestCase):
    def run_check(
        self,
        current: str | None,
        baseline: str | None,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="irlight-oom-kill-") as temporary:
            root = Path(temporary)
            current_path = root / "current.vmstat"
            baseline_path = root / "baseline.vmstat"
            if current is not None:
                current_path.write_text(current, encoding="ascii")
            if baseline is not None:
                baseline_path.write_text(baseline, encoding="ascii")

            env = dict(os.environ)
            env.pop("IRLIGHT_VMSTAT_PATH", None)
            env.pop("IRLIGHT_VMSTAT_BASELINE_PATH", None)
            return subprocess.run(
                ["bash", str(SCRIPT), str(current_path), str(baseline_path)],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )

    def test_no_delta_is_ok(self) -> None:
        current = "pgpgin 12\noom_kill 7\npgpgout 4\n"
        result = self.run_check(current, current)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            "IRLIGHT_OOM_KILL status=OK reason=none oom_kill_delta=0\n",
        )

    def test_increase_is_critical(self) -> None:
        baseline = "oom_kill 2\n"
        current = "oom_kill 5\n"
        result = self.run_check(current, baseline)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(
            result.stdout,
            "IRLIGHT_OOM_KILL status=CRITICAL reason=oom_kill_detected oom_kill_delta=3\n",
        )

    def test_counter_reset_is_unknown(self) -> None:
        result = self.run_check("oom_kill 1\n", "oom_kill 2\n")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(
            result.stdout,
            "IRLIGHT_OOM_KILL status=UNKNOWN reason=counter_reset\n",
        )

    def test_missing_inputs_are_unknown_without_path_echo(self) -> None:
        result = self.run_check(None, "oom_kill 0\n")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout,
            "IRLIGHT_OOM_KILL status=UNKNOWN reason=current_vmstat_unavailable\n",
        )
        self.assertNotIn("irlight-oom-kill-", result.stdout + result.stderr)

        result = self.run_check("oom_kill 0\n", None)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout,
            "IRLIGHT_OOM_KILL status=UNKNOWN reason=baseline_vmstat_unavailable\n",
        )
        self.assertNotIn("irlight-oom-kill-", result.stdout + result.stderr)

    def test_missing_baseline_configuration_is_unknown(self) -> None:
        env = dict(os.environ)
        env.pop("IRLIGHT_VMSTAT_BASELINE_PATH", None)
        with tempfile.TemporaryDirectory(prefix="irlight-oom-kill-") as temporary:
            current_path = Path(temporary) / "current.vmstat"
            current_path.write_text("oom_kill 0\n", encoding="ascii")
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
            "IRLIGHT_OOM_KILL status=UNKNOWN reason=baseline_vmstat_unavailable\n",
        )

    def test_environment_paths_are_supported(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-oom-kill-") as temporary:
            root = Path(temporary)
            current_path = root / "current.vmstat"
            baseline_path = root / "baseline.vmstat"
            baseline_path.write_text("oom_kill 8\n", encoding="ascii")
            current_path.write_text("oom_kill 9\n", encoding="ascii")
            env = dict(os.environ)
            env["IRLIGHT_VMSTAT_PATH"] = str(current_path)
            env["IRLIGHT_VMSTAT_BASELINE_PATH"] = str(baseline_path)
            result = subprocess.run(
                ["bash", str(SCRIPT)],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("oom_kill_delta=1", result.stdout)

    def test_missing_or_duplicate_counter_is_unknown(self) -> None:
        baseline = "oom_kill 0\n"
        for current in (
            "pgpgin 1\n",
            "oom_kill 1\noom_kill 2\n",
        ):
            with self.subTest(current=current):
                result = self.run_check(current, baseline)
                self.assertEqual(result.returncode, 3, result.stderr)

    def test_malformed_counter_is_unknown(self) -> None:
        baseline = "oom_kill 0\n"
        for current in (
            "oom_kill -1\n",
            "oom_kill nope\n",
            "oom_kill 1 extra\n",
            "oom_kill 9223372036854775808\n",
        ):
            with self.subTest(current=current):
                result = self.run_check(current, baseline)
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertEqual(
                    result.stdout,
                    "IRLIGHT_OOM_KILL status=UNKNOWN reason=invalid_vmstat_record\n",
                )

    def test_leading_zero_counter_is_decimal(self) -> None:
        result = self.run_check("oom_kill 00010\n", "oom_kill 00009\n")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("oom_kill_delta=1", result.stdout)


if __name__ == "__main__":
    unittest.main()
