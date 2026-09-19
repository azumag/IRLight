from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-host-pressure.sh"
LINK_CHECKER = ROOT / "scripts" / "check-network-link-health.sh"


class HostNetworkLinkAggregateTest(unittest.TestCase):
    def _run(
        self,
        *,
        network_link_mode: str | None = None,
        operstate: str | None = None,
        component_code: int = 0,
        configure_target: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="irlight-host-network-link-aggregate-") as temporary:
            root = Path(temporary)
            scripts = root / "scripts"
            scripts.mkdir()
            shutil.copyfile(SCRIPT, scripts / "check-host-pressure.sh")
            shutil.copyfile(LINK_CHECKER, scripts / "check-network-link-health.sh")

            component = (
                "#!/usr/bin/env bash\n"
                'exit "${IRLIGHT_TEST_COMPONENT_CODE:-0}"\n'
            )
            for name in (
                "check-disk-pressure.sh",
                "check-memory-pressure.sh",
                "check-load-pressure.sh",
                "check-psi-pressure.sh",
                "check-file-handle-pressure.sh",
                "check-conntrack-pressure.sh",
                "check-task-pressure.sh",
            ):
                (scripts / name).write_text(component, encoding="utf-8")

            interface_dir = root / "interface"
            if configure_target:
                interface_dir.mkdir()
                if operstate is not None:
                    (interface_dir / "operstate").write_text(
                        f"{operstate}\n",
                        encoding="utf-8",
                    )

            env = os.environ.copy()
            env["IRLIGHT_TEST_COMPONENT_CODE"] = str(component_code)
            if network_link_mode is not None:
                env["IRLIGHT_HOST_NETWORK_LINK_MODE"] = network_link_mode
            else:
                env.pop("IRLIGHT_HOST_NETWORK_LINK_MODE", None)
            if configure_target:
                env["IRLIGHT_NETWORK_INTERFACE_DIR"] = str(interface_dir)
            else:
                env.pop("IRLIGHT_NETWORK_INTERFACE_DIR", None)

            return subprocess.run(
                [
                    "bash",
                    str(scripts / "check-host-pressure.sh"),
                    "disk",
                    "meminfo",
                    "loadavg",
                    "4",
                    "psi",
                    "file-nr",
                    "conntrack-count",
                    "conntrack-max",
                    "threads-max",
                ],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

    def test_default_output_contract_is_unchanged(self) -> None:
        result = self._run(operstate="down")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_HOST_PRESSURE status=OK disk_status=OK memory_status=OK load_status=OK psi_status=OK file_handle_status=OK conntrack_status=OK task_status=OK",
        )
        self.assertNotIn("network_link_status", result.stdout)

    def test_enabled_up_link_is_aggregated_as_ok(self) -> None:
        result = self._run(network_link_mode="enabled", operstate="up")
        self.assertEqual(result.returncode, 0)
        self.assertIn("status=OK", result.stdout)
        self.assertIn("network_link_status=OK", result.stdout)

    def test_enabled_transitional_link_is_warning(self) -> None:
        result = self._run(network_link_mode="enabled", operstate="dormant")
        self.assertEqual(result.returncode, 1)
        self.assertIn("status=WARNING", result.stdout)
        self.assertIn("network_link_status=WARNING", result.stdout)

    def test_enabled_down_link_is_critical(self) -> None:
        result = self._run(network_link_mode="enabled", operstate="down")
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("network_link_status=CRITICAL", result.stdout)

    def test_enabled_without_target_is_unknown(self) -> None:
        result = self._run(
            network_link_mode="enabled",
            configure_target=False,
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("status=UNKNOWN", result.stdout)
        self.assertIn("network_link_status=UNKNOWN", result.stdout)

    def test_other_critical_wins_over_network_link_unknown(self) -> None:
        result = self._run(
            network_link_mode="enabled",
            operstate="unknown",
            component_code=2,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("network_link_status=UNKNOWN", result.stdout)

    def test_invalid_network_link_mode_fails_closed_before_components(self) -> None:
        result = self._run(network_link_mode="sometimes", operstate="up")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_HOST_PRESSURE status=UNKNOWN reason=invalid_network_link_mode",
        )


if __name__ == "__main__":
    unittest.main()
