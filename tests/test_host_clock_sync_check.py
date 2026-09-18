from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-host-clock-sync.py"


class HostClockSyncCheckTests(unittest.TestCase):
    def _fake_timedatectl(self, root: Path, body: str) -> Path:
        path = root / "fake-timedatectl"
        path.write_text(
            "#!/usr/bin/env python3\n" + textwrap.dedent(body),
            encoding="utf-8",
        )
        path.chmod(0o755)
        return path

    def _run(
        self,
        body: str | None,
        *,
        timeout: str = "3",
        missing_binary: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="irlight-clock-sync-") as temporary:
            root = Path(temporary)
            env = dict(os.environ)
            env["IRLIGHT_CLOCK_SYNC_TIMEOUT_SECONDS"] = timeout
            if missing_binary:
                env["IRLIGHT_TIMEDATECTL_BIN"] = str(root / "missing-timedatectl")
            else:
                assert body is not None
                env["IRLIGHT_TIMEDATECTL_BIN"] = str(
                    self._fake_timedatectl(root, body)
                )
            return subprocess.run(
                [sys.executable, str(SCRIPT)],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )

    def test_synchronized_clock_is_ok(self) -> None:
        result = self._run("print('yes')\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            "IRLIGHT_HOST_CLOCK_SYNC status=OK reason=none ntp_synchronized=yes\n",
        )

    def test_unsynchronized_clock_is_warning(self) -> None:
        result = self._run("print('no')\n")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(
            result.stdout,
            "IRLIGHT_HOST_CLOCK_SYNC status=WARNING reason=ntp_unsynchronized "
            "ntp_synchronized=no\n",
        )

    def test_invalid_output_is_unknown(self) -> None:
        for body in ("print('maybe')\n", "print('yes\\nno')\n", "pass\n"):
            with self.subTest(body=body):
                result = self._run(body)
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertEqual(
                    result.stdout,
                    "IRLIGHT_HOST_CLOCK_SYNC status=UNKNOWN "
                    "reason=invalid_timedatectl_output\n",
                )

    def test_command_failure_is_unknown_without_stderr_leak(self) -> None:
        secret = "AUDIT_DUMMY_CLOCK_SECRET"
        result = self._run(
            f"import sys\nprint('{secret}', file=sys.stderr)\nraise SystemExit(7)\n"
        )
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout,
            "IRLIGHT_HOST_CLOCK_SYNC status=UNKNOWN reason=timedatectl_failed\n",
        )
        self.assertNotIn(secret, result.stdout + result.stderr)

    def test_missing_command_is_unknown_without_path_echo(self) -> None:
        result = self._run(None, missing_binary=True)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout,
            "IRLIGHT_HOST_CLOCK_SYNC status=UNKNOWN reason=timedatectl_unavailable\n",
        )
        self.assertNotIn("irlight-clock-sync-", result.stdout + result.stderr)

    def test_timeout_is_unknown(self) -> None:
        result = self._run("import time\ntime.sleep(2)\nprint('yes')\n", timeout="1")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(
            result.stdout,
            "IRLIGHT_HOST_CLOCK_SYNC status=UNKNOWN reason=timedatectl_timeout\n",
        )

    def test_invalid_timeout_configuration_is_unknown(self) -> None:
        for timeout in ("", "0", "61", "1.5", "-1", "nope"):
            with self.subTest(timeout=timeout):
                result = self._run("print('yes')\n", timeout=timeout)
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertEqual(
                    result.stdout,
                    "IRLIGHT_HOST_CLOCK_SYNC status=UNKNOWN "
                    "reason=invalid_timeout_configuration\n",
                )


if __name__ == "__main__":
    unittest.main()
