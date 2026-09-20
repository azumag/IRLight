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

    def test_stop_terminal_boundaries_emit_static_failure_stages(self) -> None:
        stages = (
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
        )
        for stage in stages:
            with self.subTest(stage=stage):
                self.assertIn(f'emit_failure_stage "{stage}"', self.source)

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

    def test_failure_stages_are_emitted_only_on_error_paths(self) -> None:
        helper_end = self.source.index("\n}\n\ncleanup()")
        body = self.source[helper_end:]
        self.assertNotRegex(body, r'^emit_failure_stage ', msg="no unconditional stage emission")
        self.assertNotIn('emit_failure_stage "$stage"', body)


if __name__ == "__main__":
    unittest.main()
