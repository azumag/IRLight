from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-swap-io-delta.sh"


def vmstat(*, pswpin: str = "0", pswpout: str = "0") -> str:
    return f"nr_free_pages 10\npswpin {pswpin}\npswpout {pswpout}\npgfault 20\n"


class SwapIoDeltaCheckTests(unittest.TestCase):
    def _run(
        self, current: str | None, baseline: str | None
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="irlight-swap-io-") as temporary:
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
        result = self._run(
            vmstat(pswpin="10", pswpout="20"),
            vmstat(pswpin="10", pswpout="20"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            "IRLIGHT_SWAP_IO status=OK reason=none pswpin_delta=0 pswpout_delta=0\n",
        )

    def test_swap_in_delta_is_warning(self) -> None:
        result = self._run(
            vmstat(pswpin="13", pswpout="20"),
            vmstat(pswpin="10", pswpout="20"),
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(
            result.stdout,
            "IRLIGHT_SWAP_IO status=WARNING reason=swap_io_activity "
            "pswpin_delta=3 pswpout_delta=0\n",
        )

    def test_swap_out_delta_is_warning(self) -> None:
        result = self._run(
            vmstat(pswpin="10", pswpout="27"),
            vmstat(pswpin="10", pswpout="20"),
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("pswpin_delta=0 pswpout_delta=7", result.stdout)

    def test_counter_reset_is_unknown(self) -> None:
        for current in (
            vmstat(pswpin="9", pswpout="20"),
            vmstat(pswpin="10", pswpout="19"),
        ):
            with self.subTest(current=current):
                result = self._run(current, vmstat(pswpin="10", pswpout="20"))
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertEqual(
                    result.stdout,
                    "IRLIGHT_SWAP_IO status=UNKNOWN reason=counter_reset\n",
                )

    def test_missing_counter_is_unknown(self) -> None:
        result = self._run("pswpin 1\n", vmstat())
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(
            result.stdout,
            "IRLIGHT_SWAP_IO status=UNKNOWN reason=swap_io_counters_unavailable\n",
        )

    def test_duplicate_or_invalid_counter_is_unknown(self) -> None:
        malformed = (
            "pswpin 1\npswpin 2\npswpout 3\n",
            "pswpin nope\npswpout 3\n",
            "pswpin 1 extra\npswpout 3\n",
            "pswpin 9223372036854775808\npswpout 3\n",
        )
        for current in malformed:
            with self.subTest(current=current):
                result = self._run(current, vmstat())
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertEqual(
                    result.stdout,
                    "IRLIGHT_SWAP_IO status=UNKNOWN reason=invalid_vmstat_record\n",
                )

    def test_leading_zero_counters_are_supported(self) -> None:
        result = self._run(
            vmstat(pswpin="00011", pswpout="00020"),
            vmstat(pswpin="00010", pswpout="00020"),
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("pswpin_delta=1 pswpout_delta=0", result.stdout)

    def test_missing_inputs_are_unknown_without_path_echo(self) -> None:
        result = self._run(None, vmstat())
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout,
            "IRLIGHT_SWAP_IO status=UNKNOWN reason=current_vmstat_unavailable\n",
        )
        self.assertNotIn("irlight-swap-io-", result.stdout + result.stderr)

        result = self._run(vmstat(), None)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout,
            "IRLIGHT_SWAP_IO status=UNKNOWN reason=baseline_vmstat_unavailable\n",
        )
        self.assertNotIn("irlight-swap-io-", result.stdout + result.stderr)

    def test_environment_paths_are_supported(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-swap-io-") as temporary:
            root = Path(temporary)
            current_path = root / "current.vmstat"
            baseline_path = root / "baseline.vmstat"
            current_path.write_text(
                vmstat(pswpin="2", pswpout="4"), encoding="ascii"
            )
            baseline_path.write_text(
                vmstat(pswpin="1", pswpout="2"), encoding="ascii"
            )
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
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("pswpin_delta=1 pswpout_delta=2", result.stdout)

    def test_missing_baseline_configuration_is_unknown(self) -> None:
        env = dict(os.environ)
        env.pop("IRLIGHT_VMSTAT_BASELINE_PATH", None)
        with tempfile.TemporaryDirectory(prefix="irlight-swap-io-") as temporary:
            current_path = Path(temporary) / "current.vmstat"
            current_path.write_text(vmstat(), encoding="ascii")
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
            "IRLIGHT_SWAP_IO status=UNKNOWN reason=baseline_vmstat_unavailable\n",
        )


if __name__ == "__main__":
    unittest.main()
