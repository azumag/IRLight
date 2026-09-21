from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "smoke-egress-stop-terminal.sh"


class EgressStopTerminalSmokeIsolationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SCRIPT.read_text(encoding="utf-8")

    def test_compose_project_is_unique_per_run(self) -> None:
        self.assertIn(
            'smoke_project="irlight-egress-stop-terminal-smoke-$$-$RANDOM"',
            self.source,
        )
        self.assertIn(
            'compose=(docker compose -p "$smoke_project" ',
            self.source,
        )
        self.assertNotIn("COMPOSE_PROJECT_NAME", self.source)

    def test_cleanup_only_targets_generated_project(self) -> None:
        cleanup = self.source.split("cleanup() {", 1)[1].split("\n}\ntrap cleanup", 1)[0]
        self.assertIn('"${compose[@]}" down --volumes --remove-orphans', cleanup)
        self.assertNotIn("docker compose down", cleanup)
        self.assertNotIn("down -v", cleanup)

    def test_script_does_not_preemptively_stop_existing_stack(self) -> None:
        before_up = self.source.split('"${compose[@]}" up -d --build', 1)[0]
        after_trap = before_up.split("trap cleanup EXIT", 1)[1]
        self.assertNotIn('"${compose[@]}" down', after_trap)
        self.assertIn('"${compose[@]}" config >/dev/null', after_trap)

    def test_temporary_secret_material_is_private(self) -> None:
        self.assertIn("umask 077", self.source)
        self.assertIn('tmp_dir="$(mktemp -d)"', self.source)
        self.assertIn('secret_file="$tmp_dir/egress_url"', self.source)
        self.assertIn('chmod 600 "$secret_file"', self.source)

    def test_reconnecting_timeout_emits_allowlisted_secret_safe_evidence(self) -> None:
        helper = self.source.split("emit_reconnect_timeout_evidence() {", 1)[1].split(
            "\n}\n\nwait_egress_status() {", 1
        )[0]
        self.assertIn('payload="$(read_egress_status)"', helper)
        self.assertIn('gateway_state="not-running"', helper)
        self.assertIn("value = json.load(sys.stdin)", helper)
        self.assertIn('status = status_value if status_value in allowed_statuses else "OTHER"', helper)
        self.assertIn('reason = "OTHER"', helper)
        self.assertIn('next_retry_present = "yes"', helper)
        self.assertIn("IRLIGHT_EGRESS_STOP_TERMINAL_RECONNECT_EVIDENCE", helper)
        self.assertIn("GITHUB_STEP_SUMMARY", helper)
        self.assertNotIn("redaction_values_file", helper)
        self.assertNotIn("secret_file", helper)
        self.assertNotIn("stream_key", helper)
        self.assertNotIn("print(value)", helper)
        self.assertNotIn("print(payload)", helper)

        failure = self.source.split(
            'if ! wait_egress_status RECONNECTING 45; then', 1
        )[1].split("fi", 1)[0]
        evidence_position = failure.index("emit_reconnect_timeout_evidence")
        stage_position = failure.index('emit_failure_stage "reconnecting"')
        self.assertLess(evidence_position, stage_position)

    def test_reconnect_evidence_fails_closed_for_unreadable_status(self) -> None:
        helper = self.source.split("emit_reconnect_timeout_evidence() {", 1)[1].split(
            "\n}\n\nwait_egress_status() {", 1
        )[0]
        self.assertIn("except Exception:\n    raise SystemExit(2)", helper)
        self.assertIn("status=UNREADABLE reason=UNREADABLE", helper)
        self.assertIn('gateway=$gateway_state', helper)

    def test_reconnect_timeout_stack_dump_is_opt_in_and_handler_guarded(self) -> None:
        self.assertIn('EGRESS_STACK_SIGNAL_DIAGNOSTICS: "1"', self.source)
        helper = self.source.split("request_egress_stack_dump() {", 1)[1].split(
            "\n}\n\nemit_reconnect_timeout_evidence() {", 1
        )[0]
        self.assertIn('ps --status running --services', helper)
        self.assertIn('IRLIGHT_EGRESS_STACK_SIGNAL_READY signal=SIGUSR2', helper)
        self.assertIn('kill -s SIGUSR2 egress-gateway', helper)
        self.assertIn('IRLIGHT_EGRESS_STACK_DUMP_SKIPPED reason=gateway-not-running', helper)
        self.assertIn('IRLIGHT_EGRESS_STACK_DUMP_SKIPPED reason=handler-unconfirmed', helper)
        self.assertIn('IRLIGHT_EGRESS_STACK_DUMP_SKIPPED reason=signal-failed', helper)
        self.assertIn('IRLIGHT_EGRESS_STACK_DUMP_REQUESTED signal=SIGUSR2', helper)
        self.assertIn('redact_generated_secrets', helper)
        self.assertNotIn("secret_file", helper)
        self.assertNotIn("stream_key", helper)

        failure = self.source.split(
            'if ! wait_egress_status RECONNECTING 45; then', 1
        )[1].split("fi", 1)[0]
        stack_position = failure.index("request_egress_stack_dump")
        evidence_position = failure.index("emit_reconnect_timeout_evidence")
        stage_position = failure.index('emit_failure_stage "reconnecting"')
        self.assertLess(stack_position, evidence_position)
        self.assertLess(evidence_position, stage_position)

    def test_failure_cleanup_retains_large_redacted_gateway_log_window(self) -> None:
        cleanup = self.source.split("cleanup() {", 1)[1].split("\n}\ntrap cleanup", 1)[0]
        self.assertIn("emit_redacted_compose_logs egress-gateway 400", cleanup)


if __name__ == "__main__":
    unittest.main()
