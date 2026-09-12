from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-network-link-health.sh"


class NetworkLinkHealthCheckTest(unittest.TestCase):
    def _run(self, operstate: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="irlight-network-link-") as temporary:
            interface_dir = Path(temporary)
            (interface_dir / "operstate").write_text(operstate, encoding="utf-8")
            return subprocess.run(
                ["bash", str(SCRIPT), str(interface_dir)],
                text=True,
                capture_output=True,
                check=False,
            )

    def test_up_is_ok(self) -> None:
        result = self._run("up\n")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_NETWORK_LINK_HEALTH status=OK operstate=up",
        )

    def test_dormant_and_testing_are_warning(self) -> None:
        for state in ("dormant", "testing"):
            with self.subTest(state=state):
                result = self._run(f"{state}\n")
                self.assertEqual(result.returncode, 1)
                self.assertEqual(
                    result.stdout.strip(),
                    f"IRLIGHT_NETWORK_LINK_HEALTH status=WARNING operstate={state}",
                )

    def test_unavailable_states_are_critical(self) -> None:
        for state in ("down", "lowerlayerdown", "notpresent"):
            with self.subTest(state=state):
                result = self._run(f"{state}\n")
                self.assertEqual(result.returncode, 2)
                self.assertEqual(
                    result.stdout.strip(),
                    f"IRLIGHT_NETWORK_LINK_HEALTH status=CRITICAL operstate={state}",
                )

    def test_kernel_unknown_state_fails_closed(self) -> None:
        result = self._run("unknown\n")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_NETWORK_LINK_HEALTH status=UNKNOWN reason=operstate_unknown",
        )

    def test_malformed_or_multiline_state_is_unknown(self) -> None:
        for text in ("", "UP\n", "up\ndown\n", "up \n"):
            with self.subTest(text=text):
                result = self._run(text)
                self.assertEqual(result.returncode, 3)
                self.assertEqual(
                    result.stdout.strip(),
                    "IRLIGHT_NETWORK_LINK_HEALTH status=UNKNOWN reason=invalid_operstate",
                )

    def test_target_is_required(self) -> None:
        env = {
            key: value
            for key, value in os.environ.items()
            if key != "IRLIGHT_NETWORK_INTERFACE_DIR"
        }
        result = subprocess.run(
            ["bash", str(SCRIPT)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_NETWORK_LINK_HEALTH status=UNKNOWN reason=target_required",
        )

    def test_environment_target_is_supported(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-network-link-env-") as temporary:
            interface_dir = Path(temporary)
            (interface_dir / "operstate").write_text("up\n", encoding="utf-8")
            env = os.environ.copy()
            env["IRLIGHT_NETWORK_INTERFACE_DIR"] = str(interface_dir)
            result = subprocess.run(
                ["bash", str(SCRIPT)],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0)
        self.assertIn("status=OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
