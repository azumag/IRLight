from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "smoke-egress-stop-terminal.sh"


class EgressStopTerminalSmokeHardeningTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SCRIPT.read_text(encoding="utf-8")

    def test_failure_stage_annotation_matches_ci_suite_contract(self) -> None:
        self.assertIn(
            "::error title=IRLight docker smoke failure::stage=%s",
            self.source,
        )

    def test_stop_terminal_boundaries_emit_only_expected_static_failure_stages(self) -> None:
        stages = {
            "compose-config",
            "compose-up",
            "initial-connected",
            "target-stop",
            "reconnecting",
            "backoff-window",
            "gateway-stop",
            "stopped-user-stopped",
            "gateway-still-running",
            "continuity-survives-stop",
            "target-recovery-start",
            "target-recovery-no-restart",
            "target-recovery-stopped-status",
            "unsafe-destination-terminal",
            "unsafe-destination-failed",
            "unsafe-destination-reason",
            "secret-redaction-terminal-output",
            "secret-redaction-logs",
        }
        calls = re.findall(
            r'^\s*emit_failure_stage "([a-z0-9-]+)"\s*$',
            self.source,
            flags=re.MULTILINE,
        )
        # A single semantic stage may legitimately guard more than one assertion
        # (for example STOPPED and USER_STOPPED). Reject missing or unexpected
        # stages without requiring each token to appear exactly once.
        self.assertEqual(set(calls), stages)
        self.assertIsNone(
            re.search(r'emit_failure_stage\s+"\$', self.source),
            "failure-stage call sites must stay hard-coded to avoid workflow-command injection",
        )

    def test_existing_stop_and_status_contracts_remain_bounded(self) -> None:
        expected_contracts = (
            "wait_egress_status CONNECTED 60",
            "wait_egress_status RECONNECTING 45",
            'stop -t 5 egress-gateway',
            "wait_egress_status STOPPED 10",
            "assert_status_reason STOPPED USER_STOPPED",
            "wait_egress_status FAILED 5",
            "assert_status_reason FAILED DESTINATION_UNSAFE",
        )
        for contract in expected_contracts:
            with self.subTest(contract=contract):
                self.assertIn(contract, self.source)


if __name__ == "__main__":
    unittest.main()
