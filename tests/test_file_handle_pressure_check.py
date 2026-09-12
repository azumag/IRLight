from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-file-handle-pressure.sh"


class FileHandlePressureCheckTest(unittest.TestCase):
    def _run(
        self,
        content: str = "100 0 1000\n",
        *,
        env_overrides: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="irlight-file-handle-pressure-") as temporary:
            file_nr = Path(temporary) / "file-nr"
            file_nr.write_text(content, encoding="utf-8")
            env = os.environ.copy()
            if env_overrides:
                env.update(env_overrides)
            return subprocess.run(
                ["bash", str(SCRIPT), str(file_nr)],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

    def test_ok_below_default_thresholds(self) -> None:
        result = self._run()
        self.assertEqual(result.returncode, 0)
        self.assertIn("status=OK", result.stdout)
        self.assertIn("usage_percent=10", result.stdout)

    def test_warning_boundary(self) -> None:
        result = self._run("800 0 1000\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("status=WARNING", result.stdout)

    def test_critical_boundary(self) -> None:
        result = self._run("900 0 1000\n")
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)

    def test_unused_allocated_handles_are_not_counted_as_active(self) -> None:
        result = self._run("900 200 1000\n")
        self.assertEqual(result.returncode, 0)
        self.assertIn("active=700", result.stdout)
        self.assertIn("usage_percent=70", result.stdout)

    def test_thresholds_are_decimal_and_configurable(self) -> None:
        result = self._run(
            "85 0 100\n",
            env_overrides={
                "IRLIGHT_FILE_HANDLE_WARNING_PERCENT": "080",
                "IRLIGHT_FILE_HANDLE_CRITICAL_PERCENT": "090",
            },
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("warning_percent=80", result.stdout)

    def test_linux_signed_long_maximum_is_supported(self) -> None:
        result = self._run("1 0 9223372036854775807\n")
        self.assertEqual(result.returncode, 0)
        self.assertIn("maximum=9223372036854775807", result.stdout)

    def test_invalid_threshold_is_unknown(self) -> None:
        result = self._run(
            env_overrides={"IRLIGHT_FILE_HANDLE_WARNING_PERCENT": "101"},
        )
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_FILE_HANDLE_PRESSURE status=UNKNOWN reason=invalid_threshold",
        )

    def test_malformed_file_nr_is_unknown(self) -> None:
        for content in (
            "100 0\n",
            "100 zero 1000\n",
            "100 0 1000 extra\n",
            "100 0 1000\n200 0 1000\n",
            "100 101 1000\n",
            "1001 0 1000\n",
            "100 0 0\n",
            "9223372036854775808 0 9223372036854775808\n",
        ):
            with self.subTest(content=content):
                result = self._run(content)
                self.assertEqual(result.returncode, 3)
                self.assertEqual(
                    result.stdout.strip(),
                    "IRLIGHT_FILE_HANDLE_PRESSURE status=UNKNOWN reason=invalid_file_nr",
                )


if __name__ == "__main__":
    unittest.main()
