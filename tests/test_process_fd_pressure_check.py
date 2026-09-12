from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-process-fd-pressure.sh"


class ProcessFdPressureCheckTest(unittest.TestCase):
    def _run(
        self,
        *,
        open_fds: int = 10,
        soft_limit: str = "100",
        hard_limit: str = "200",
        limits_text: str | None = None,
        env_overrides: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="irlight-process-fd-pressure-") as temporary:
            root = Path(temporary)
            fd_dir = root / "fd"
            fd_dir.mkdir()
            for descriptor in range(open_fds):
                (fd_dir / str(descriptor)).touch()
            limits_path = root / "limits"
            limits_path.write_text(
                limits_text
                if limits_text is not None
                else f"Max open files            {soft_limit}                 {hard_limit}                 files\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            if env_overrides:
                env.update(env_overrides)
            return subprocess.run(
                ["bash", str(SCRIPT), str(fd_dir), str(limits_path)],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

    def test_ok_below_default_thresholds(self) -> None:
        result = self._run(open_fds=10)
        self.assertEqual(result.returncode, 0)
        self.assertIn("status=OK", result.stdout)
        self.assertIn("usage_percent=10", result.stdout)
        self.assertIn("open_fds=10", result.stdout)

    def test_warning_boundary(self) -> None:
        result = self._run(open_fds=80)
        self.assertEqual(result.returncode, 1)
        self.assertIn("status=WARNING", result.stdout)
        self.assertIn("usage_percent=80", result.stdout)

    def test_critical_boundary(self) -> None:
        result = self._run(open_fds=90)
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("usage_percent=90", result.stdout)

    def test_zero_soft_limit_is_critical(self) -> None:
        result = self._run(open_fds=0, soft_limit="0", hard_limit="100")
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("usage_percent=NO_HEADROOM", result.stdout)

    def test_count_above_lowered_soft_limit_is_critical(self) -> None:
        result = self._run(open_fds=11, soft_limit="10", hard_limit="100")
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("usage_percent=OVER_LIMIT", result.stdout)

    def test_unlimited_soft_limit_has_no_local_percentage(self) -> None:
        result = self._run(open_fds=10, soft_limit="unlimited", hard_limit="unlimited")
        self.assertEqual(result.returncode, 0)
        self.assertIn("status=OK", result.stdout)
        self.assertIn("usage_percent=NA", result.stdout)
        self.assertIn("soft_limit=unlimited", result.stdout)

    def test_thresholds_are_decimal_and_configurable(self) -> None:
        result = self._run(
            open_fds=85,
            env_overrides={
                "IRLIGHT_PROCESS_FD_WARNING_PERCENT": "080",
                "IRLIGHT_PROCESS_FD_CRITICAL_PERCENT": "090",
            },
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("warning_percent=80", result.stdout)

    def test_invalid_threshold_is_unknown(self) -> None:
        result = self._run(
            env_overrides={"IRLIGHT_PROCESS_FD_WARNING_PERCENT": "101"}
        )
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_PROCESS_FD_PRESSURE status=UNKNOWN reason=invalid_threshold",
        )

    def test_malformed_limits_are_unknown(self) -> None:
        for limits_text in (
            "",
            "Max open files 100 files\n",
            "Max open files one 200 files\n",
            "Max open files unlimited 200 files\n",
            "Max open files 300 200 files\n",
            "Max open files 100 200 bytes\n",
            "Max open files 100 200 files\nMax open files 100 200 files\n",
            "Max open files 9223372036854775808 unlimited files\n",
        ):
            with self.subTest(limits_text=limits_text):
                result = self._run(limits_text=limits_text)
                self.assertEqual(result.returncode, 3)
                self.assertEqual(
                    result.stdout.strip(),
                    "IRLIGHT_PROCESS_FD_PRESSURE status=UNKNOWN reason=invalid_limits",
                )

    def test_target_is_required(self) -> None:
        result = subprocess.run(
            ["bash", str(SCRIPT)],
            env={
                key: value
                for key, value in os.environ.items()
                if key not in {"IRLIGHT_PROCESS_FD_DIR", "IRLIGHT_PROCESS_LIMITS_PATH"}
            },
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_PROCESS_FD_PRESSURE status=UNKNOWN reason=target_required",
        )


if __name__ == "__main__":
    unittest.main()
