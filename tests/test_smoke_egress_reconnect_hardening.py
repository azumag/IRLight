from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "smoke-egress-reconnect.sh"


class EgressReconnectSmokeHardeningTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SCRIPT.read_text(encoding="utf-8")

    def test_compose_project_is_unique_per_run(self) -> None:
        self.assertIn(
            'smoke_project="irlight-egress-reconnect-smoke-$$-$RANDOM"',
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
        self.assertNotIn('"${compose[@]}" down', before_up.split("trap cleanup EXIT", 1)[1])
        self.assertIn('"${compose[@]}" config >/dev/null', before_up)

    def test_runtime_secret_is_private_per_run(self) -> None:
        self.assertIn("umask 077", self.source)
        self.assertIn('secret_file="$tmp_dir/egress_url"', self.source)
        self.assertIn('chmod 600 "$secret_file"', self.source)

    def test_status_wait_performs_final_observation(self) -> None:
        wait = self.source.split("wait_egress_status() {", 1)[1].split(
            "\n}\n\ntarget_path_ready()", 1
        )[0]
        self.assertGreaterEqual(wait.count('egress_status_matches "$expected"'), 2)
        self.assertIn("Observe it once more before failing", wait)

    def test_target_path_wait_performs_final_observation(self) -> None:
        wait = self.source.split("wait_target_path() {", 1)[1].split(
            "\n}\n\n# The generated project", 1
        )[0]
        self.assertGreaterEqual(wait.count("target_path_ready"), 2)
        self.assertIn("final-observation rule", wait)

    def test_failure_stage_annotation_matches_ci_suite_contract(self) -> None:
        self.assertIn(
            "::error title=IRLight docker smoke failure::stage=%s",
            self.source,
        )

    def test_reconnect_boundaries_emit_static_failure_stages(self) -> None:
        stages = (
            "egress-connected-initial",
            "target-path-initial",
            "target-stop",
            "egress-reconnecting",
            "continuity-during-outage",
            "target-start",
            "target-api-recovery",
            "egress-connected-recovery",
            "target-path-recovery",
            "continuity-after-recovery",
        )
        for stage in stages:
            with self.subTest(stage=stage):
                self.assertIn(f'emit_failure_stage "{stage}"', self.source)

        self.assertIsNone(
            re.search(r'emit_failure_stage\s+"\$', self.source),
            "failure-stage call sites must stay hard-coded to avoid workflow-command injection",
        )

    def test_log_redaction_drains_compose_logs_before_searching(self) -> None:
        redaction = self.source.split('status_payload="$(read_egress_status)"', 1)[1].split(
            "\n# Simulate a remote RTMP outage", 1
        )[0]
        self.assertIn('egress_logs_file="$tmp_dir/egress-gateway.log"', redaction)
        self.assertIn(
            'if ! "${compose[@]}" logs --no-color egress-gateway >"$egress_logs_file"; then',
            redaction,
        )
        self.assertIn('if grep -Fq "$stream_key" "$egress_logs_file"; then', redaction)
        self.assertNotRegex(
            redaction,
            r'logs\s+--no-color\s+egress-gateway\s*\|\s*grep\s+-Fq',
        )

    def test_log_redaction_fails_closed_when_log_read_fails(self) -> None:
        redaction = self.source.split('egress_logs_file="$tmp_dir/egress-gateway.log"', 1)[1].split(
            "\n# Simulate a remote RTMP outage", 1
        )[0]
        read_failure = redaction.split(
            'if ! "${compose[@]}" logs --no-color egress-gateway >"$egress_logs_file"; then',
            1,
        )[1].split("\nfi", 1)[0]
        self.assertIn('emit_failure_stage "secret-redaction-logs-read"', read_failure)
        self.assertIn("exit 1", read_failure)
        self.assertNotIn("$stream_key", read_failure)

    def test_failure_diagnostics_redact_generated_stream_key(self) -> None:
        helper = self.source.split("redact_stream_key() {", 1)[1].split(
            "\n}\n\ncleanup()", 1
        )[0]
        self.assertIn('.replace(secret, "<redacted>")', helper)

        cleanup = self.source.split("cleanup() {", 1)[1].split("\n}\ntrap cleanup", 1)[0]
        for service in ("continuity", "egress-gateway", "egress-target"):
            with self.subTest(service=service):
                self.assertRegex(
                    cleanup,
                    rf'logs --no-color --tail=\d+ {service} 2>&1 \| redact_stream_key >&2 \|\| true',
                )

        status_wait = self.source.split("wait_egress_status() {", 1)[1].split(
            "\n}\n\ntarget_path_ready()", 1
        )[0]
        self.assertIn("read_egress_status | redact_stream_key", status_wait)
        self.assertNotIn("last=$(read_egress_status)", status_wait)

        target_wait = self.source.split("wait_target_path() {", 1)[1].split(
            "\n}\n\n# The generated project", 1
        )[0]
        self.assertNotIn("live/$stream_key", target_wait)
        self.assertNotIn("paths=$(target_api", target_wait)


if __name__ == "__main__":
    unittest.main()
