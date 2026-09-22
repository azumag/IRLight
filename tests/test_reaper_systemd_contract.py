from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVICE_PATH = ROOT / "deploy" / "systemd" / "irlight-reaper.service"
TIMER_PATH = ROOT / "deploy" / "systemd" / "irlight-reaper.timer"
RUNBOOK_PATH = ROOT / "docs" / "conoha-runtime-verification.md"


def _directive(text: str, key: str) -> str:
    match = re.search(rf"^{re.escape(key)}=(.+)$", text, flags=re.MULTILINE)
    if match is None:
        raise AssertionError(f"missing systemd directive: {key}")
    return match.group(1).strip()


def _duration_seconds(value: str) -> float:
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(ms|s|min|h)", value)
    if match is None:
        raise AssertionError(f"unsupported systemd duration in contract test: {value}")
    amount = float(match.group(1))
    unit = match.group(2)
    multiplier = {"ms": 0.001, "s": 1.0, "min": 60.0, "h": 3600.0}[unit]
    return amount * multiplier


class ReaperSystemdContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = SERVICE_PATH.read_text(encoding="utf-8")
        self.timer = TIMER_PATH.read_text(encoding="utf-8")
        self.runbook = RUNBOOK_PATH.read_text(encoding="utf-8")

    def test_service_reuses_running_control_plane_container(self) -> None:
        self.assertEqual(_directive(self.service, "Type"), "oneshot")
        command = _directive(self.service, "ExecStart")
        self.assertEqual(
            command,
            "/usr/bin/docker compose -f /opt/irlight/docker-compose.poc.yml "
            "exec -T control-ui python /app/reaper_cli.py",
        )
        self.assertEqual(_directive(self.service, "WorkingDirectory"), "/opt/irlight")
        self.assertNotIn(" run ", f" {command} ")
        self.assertNotIn("--renew-anon-volumes", command)
        self.assertNotIn(" down ", f" {command} ")

    def test_timer_is_faster_than_default_provisioning_timeout(self) -> None:
        interval = _duration_seconds(_directive(self.timer, "OnUnitActiveSec"))
        default_provisioning_timeout = 600.0
        self.assertGreater(interval, 0.0)
        self.assertLess(interval, default_provisioning_timeout)
        self.assertEqual(_directive(self.timer, "Unit"), "irlight-reaper.service")
        self.assertEqual(_directive(self.timer, "Persistent").lower(), "true")

    def test_service_has_a_bounded_execution_window(self) -> None:
        timeout = _duration_seconds(_directive(self.service, "TimeoutStartSec"))
        interval = _duration_seconds(_directive(self.timer, "OnUnitActiveSec"))
        self.assertGreater(timeout, 0.0)
        self.assertLess(timeout, interval)

    def test_runbook_installs_and_verifies_the_same_units(self) -> None:
        for expected in (
            "deploy/systemd/irlight-reaper.service",
            "deploy/systemd/irlight-reaper.timer",
            "systemctl enable --now irlight-reaper.timer",
            "systemctl list-timers irlight-reaper.timer",
            "systemctl start irlight-reaper.service",
            "journalctl -u irlight-reaper.service",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, self.runbook)
        self.assertIn("同じ `STATE_DIR`", self.runbook)
        self.assertIn("料金が", self.runbook)


if __name__ == "__main__":
    unittest.main()
