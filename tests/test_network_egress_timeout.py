from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-network-egress-health.sh"


class NetworkEgressTimeoutTest(unittest.TestCase):
    def _run(
        self,
        *,
        timeout_seconds: str,
        link_body: str,
        ipv4_body: str,
    ) -> tuple[subprocess.CompletedProcess[str], float, Path]:
        temporary = tempfile.TemporaryDirectory(prefix="irlight-network-egress-timeout-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        scripts = root / "scripts"
        scripts.mkdir()
        aggregate = scripts / SCRIPT.name
        aggregate.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")

        (scripts / "check-network-link-health.sh").write_text(link_body, encoding="utf-8")
        (scripts / "check-ipv4-default-route.sh").write_text(ipv4_body, encoding="utf-8")
        (scripts / "check-ipv6-default-route.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")

        marker = root / "component-ran"
        env = os.environ.copy()
        env["IRLIGHT_NETWORK_COMPONENT_TIMEOUT_SECONDS"] = timeout_seconds
        env["IRLIGHT_TEST_COMPONENT_MARKER"] = str(marker)

        started = time.monotonic()
        result = subprocess.run(
            ["bash", str(aggregate), "eth0", "ipv4"],
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=6,
        )
        elapsed = time.monotonic() - started
        return result, elapsed, marker

    def test_wedged_component_becomes_unknown_and_later_critical_still_wins(self) -> None:
        result, elapsed, _ = self._run(
            timeout_seconds="1",
            link_body="#!/usr/bin/env bash\nsleep 5\nexit 0\n",
            ipv4_body="#!/usr/bin/env bash\nexit 2\n",
        )
        self.assertEqual(result.returncode, 2)
        self.assertLess(elapsed, 4)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("link_status=UNKNOWN", result.stdout)
        self.assertIn("ipv4_route_status=CRITICAL", result.stdout)
        self.assertIn("ipv6_route_status=NOT_REQUIRED", result.stdout)

    def test_invalid_timeout_fails_closed_without_running_components(self) -> None:
        component_body = (
            "#!/usr/bin/env bash\n"
            "printf ran >> \"$IRLIGHT_TEST_COMPONENT_MARKER\"\n"
            "exit 0\n"
        )
        result, elapsed, marker = self._run(
            timeout_seconds="0",
            link_body=component_body,
            ipv4_body=component_body,
        )
        self.assertEqual(result.returncode, 3)
        self.assertLess(elapsed, 2)
        self.assertFalse(marker.exists())
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_NETWORK_EGRESS_HEALTH status=UNKNOWN link_status=UNKNOWN "
            "ipv4_route_status=UNKNOWN ipv6_route_status=NOT_REQUIRED family=ipv4",
        )


if __name__ == "__main__":
    unittest.main()
