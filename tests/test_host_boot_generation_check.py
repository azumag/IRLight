from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-host-boot-generation.sh"


class HostBootGenerationCheckTests(unittest.TestCase):
    def run_check(
        self,
        current: str | None,
        baseline: str | None,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="irlight-boot-generation-") as temporary:
            root = Path(temporary)
            current_path = root / "current.boot_id"
            baseline_path = root / "baseline.boot_id"
            if current is not None:
                current_path.write_text(current, encoding="ascii")
            if baseline is not None:
                baseline_path.write_text(baseline, encoding="ascii")

            env = dict(os.environ)
            env.pop("IRLIGHT_BOOT_ID_PATH", None)
            env.pop("IRLIGHT_BOOT_ID_BASELINE_PATH", None)
            return subprocess.run(
                ["bash", str(SCRIPT), str(current_path), str(baseline_path)],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )

    def test_same_boot_is_ok(self) -> None:
        boot_id = "12345678-1234-4abc-8def-1234567890ab\n"
        result = self.run_check(boot_id, boot_id)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            "IRLIGHT_BOOT_GENERATION status=OK reason=same_boot\n",
        )

    def test_single_line_without_trailing_newline_is_supported(self) -> None:
        boot_id = "12345678-1234-4abc-8def-1234567890ab"
        result = self.run_check(boot_id, boot_id)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            "IRLIGHT_BOOT_GENERATION status=OK reason=same_boot\n",
        )

    def test_hex_case_does_not_create_false_change(self) -> None:
        result = self.run_check(
            "ABCDEF12-3456-4ABC-8DEF-1234567890AB\n",
            "abcdef12-3456-4abc-8def-1234567890ab\n",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            "IRLIGHT_BOOT_GENERATION status=OK reason=same_boot\n",
        )

    def test_changed_boot_is_warning(self) -> None:
        result = self.run_check(
            "12345678-1234-4abc-8def-1234567890ab\n",
            "87654321-4321-4cba-8fed-ba0987654321\n",
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(
            result.stdout,
            "IRLIGHT_BOOT_GENERATION status=WARNING reason=boot_generation_changed\n",
        )

    def test_missing_inputs_are_unknown_without_path_echo(self) -> None:
        valid = "12345678-1234-4abc-8def-1234567890ab\n"

        result = self.run_check(None, valid)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout,
            "IRLIGHT_BOOT_GENERATION status=UNKNOWN reason=current_boot_id_unavailable\n",
        )
        self.assertNotIn("irlight-boot-generation-", result.stdout + result.stderr)

        result = self.run_check(valid, None)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout,
            "IRLIGHT_BOOT_GENERATION status=UNKNOWN reason=baseline_boot_id_unavailable\n",
        )
        self.assertNotIn("irlight-boot-generation-", result.stdout + result.stderr)

    def test_missing_baseline_configuration_is_unknown(self) -> None:
        env = dict(os.environ)
        env.pop("IRLIGHT_BOOT_ID_BASELINE_PATH", None)
        with tempfile.TemporaryDirectory(prefix="irlight-boot-generation-") as temporary:
            current_path = Path(temporary) / "current.boot_id"
            current_path.write_text(
                "12345678-1234-4abc-8def-1234567890ab\n",
                encoding="ascii",
            )
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
            "IRLIGHT_BOOT_GENERATION status=UNKNOWN reason=baseline_boot_id_unavailable\n",
        )

    def test_environment_paths_are_supported(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-boot-generation-") as temporary:
            root = Path(temporary)
            current_path = root / "current.boot_id"
            baseline_path = root / "baseline.boot_id"
            current_path.write_text(
                "12345678-1234-4abc-8def-1234567890ab\n",
                encoding="ascii",
            )
            baseline_path.write_text(
                "87654321-4321-4cba-8fed-ba0987654321\n",
                encoding="ascii",
            )
            env = dict(os.environ)
            env["IRLIGHT_BOOT_ID_PATH"] = str(current_path)
            env["IRLIGHT_BOOT_ID_BASELINE_PATH"] = str(baseline_path)
            result = subprocess.run(
                ["bash", str(SCRIPT)],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("reason=boot_generation_changed", result.stdout)

    def test_malformed_ids_are_unknown(self) -> None:
        valid = "12345678-1234-4abc-8def-1234567890ab\n"
        for current in (
            "",
            "not-a-uuid\n",
            "12345678-1234-4abc-8def-1234567890ab extra\n",
            "12345678-1234-4abc-8def-1234567890ab\nsecond-line\n",
        ):
            with self.subTest(current=current):
                result = self.run_check(current, valid)
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertEqual(
                    result.stdout,
                    "IRLIGHT_BOOT_GENERATION status=UNKNOWN reason=invalid_current_boot_id\n",
                )

        for baseline in (
            "not-a-uuid\n",
            "12345678-1234-4abc-8def-1234567890ab\nsecond-line\n",
        ):
            with self.subTest(baseline=baseline):
                result = self.run_check(valid, baseline)
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertEqual(
                    result.stdout,
                    "IRLIGHT_BOOT_GENERATION status=UNKNOWN reason=invalid_baseline_boot_id\n",
                )


if __name__ == "__main__":
    unittest.main()
