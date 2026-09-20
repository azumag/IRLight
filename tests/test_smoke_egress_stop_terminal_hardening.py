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
            "secret-redaction-logs-read",
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

    def test_failure_cleanup_redacts_generated_secrets_before_emitting_logs(self) -> None:
        self.assertIn("redact_generated_secrets()", self.source)
        self.assertIn("emit_redacted_compose_logs()", self.source)
        self.assertIn("emit_redacted_compose_logs continuity 120 || true", self.source)
        self.assertIn("emit_redacted_compose_logs egress-gateway 160 || true", self.source)
        self.assertIn("emit_redacted_compose_logs egress-target 120 || true", self.source)
        self.assertIn('"$stream_key" "$unsafe_secret"', self.source)
        self.assertNotIn(
            '"${compose[@]}" logs --no-color --tail=160 egress-gateway >&2 || true',
            self.source,
        )
        self.assertNotIn(
            '"${compose[@]}" logs --no-color --tail=120 continuity >&2 || true',
            self.source,
        )
        self.assertNotIn(
            '"${compose[@]}" logs --no-color --tail=120 egress-target >&2 || true',
            self.source,
        )

    def test_secret_log_check_fails_closed_when_log_read_fails(self) -> None:
        self.assertIn('egress_logs_file="$tmp_dir/egress-gateway.log"', self.source)
        self.assertIn(
            'if ! "${compose[@]}" logs --no-color egress-gateway >"$egress_logs_file" 2>&1; then',
            self.source,
        )
        self.assertIn('emit_failure_stage "secret-redaction-logs-read"', self.source)
        self.assertNotIn(
            '"${compose[@]}" logs --no-color egress-gateway 2>/dev/null || true',
            self.source,
        )

    def test_status_and_terminal_failure_diagnostics_do_not_emit_raw_payloads(self) -> None:
        self.assertIn(
            "printf '%s' \"$payload\" | redact_generated_secrets",
            self.source,
        )
        self.assertIn(
            "printf '%s\\n' \"$terminal_output\" | redact_generated_secrets",
            self.source,
        )
        self.assertNotIn('echo "$terminal_output" >&2', self.source)
        self.assertNotIn('assert value.get("status") == expected_status, value', self.source)
        self.assertNotIn('assert value.get("reason_code") == expected_reason, value', self.source)


if __name__ == "__main__":
    unittest.main()
