from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "smoke-egress-publish-conflict.sh"


class EgressPublishConflictSmokeIsolationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SCRIPT.read_text(encoding="utf-8")

    def test_compose_project_is_unique_per_run(self) -> None:
        self.assertIn(
            'smoke_project="irlight-egress-publish-conflict-smoke-$$-$RANDOM"',
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
        before_up = self.source.split(
            '"${compose[@]}" up -d mediamtx continuity control-ui node-agent conflict-holder',
            1,
        )[0]
        after_trap = before_up.split("trap cleanup EXIT", 1)[1]
        self.assertNotIn('"${compose[@]}" down', after_trap)
        self.assertIn('"${compose[@]}" config >/dev/null', after_trap)

    def test_temporary_secret_material_is_private(self) -> None:
        self.assertIn("umask 077", self.source)
        self.assertIn('tmp_dir="$(mktemp -d)"', self.source)
        self.assertIn('secret_file="$tmp_dir/egress_url"', self.source)
        self.assertIn('chmod 600 "$secret_file"', self.source)

    def test_failure_diagnostics_redact_generated_stream_key_without_raw_fallback(
        self,
    ) -> None:
        self.assertIn("redact_stream_key() {", self.source)
        helper = self.source.split("emit_redacted_compose_logs() {", 1)[1].split(
            "\n}\n\ncleanup() {", 1
        )[0]
        self.assertIn('>"$raw_file" 2>&1 || logs_rc=$?', helper)
        self.assertIn('redact_stream_key <"$raw_file" >&2', helper)
        self.assertIn("output withheld", helper)

        cleanup = self.source.split("cleanup() {", 1)[1].split("\n}\ntrap cleanup", 1)[0]
        self.assertIn("emit_redacted_compose_logs conflict-holder 120", cleanup)
        self.assertIn("emit_redacted_compose_logs egress-conflict 160", cleanup)
        self.assertIn("emit_redacted_compose_logs egress-conflict-target 160", cleanup)
        self.assertNotIn(
            '"${compose[@]}" logs --no-color --tail=120 conflict-holder >&2', cleanup
        )
        self.assertNotIn(
            '"${compose[@]}" logs --no-color --tail=160 egress-conflict >&2', cleanup
        )
        self.assertNotIn(
            '"${compose[@]}" logs --no-color --tail=160 egress-conflict-target >&2',
            cleanup,
        )

    def test_secret_assertion_fails_closed_on_log_read_and_avoids_pipefail_q(
        self,
    ) -> None:
        final_checks = self.source.split('status_payload="$(read_status)"', 1)[1]
        self.assertIn(
            'egress_logs_file="$tmp_dir/egress-conflict.log"', final_checks
        )
        self.assertIn(
            '"${compose[@]}" logs --no-color egress-conflict >"$egress_logs_file" 2>&1',
            final_checks,
        )
        self.assertIn('grep -Fq "$stream_key" "$egress_logs_file"', final_checks)
        self.assertNotIn(
            '"${compose[@]}" logs --no-color egress-conflict | grep -Fq "$stream_key"',
            final_checks,
        )

    def test_conflict_target_is_ready_before_holder_starts(self) -> None:
        target_up = self.source.index(
            '"${compose[@]}" up -d egress-conflict-target'
        )
        listener_wait = self.source.index("wait_for_target_listener 30", target_up)
        holder_up = self.source.index(
            '"${compose[@]}" up -d mediamtx continuity control-ui node-agent conflict-holder'
        )
        self.assertLess(target_up, listener_wait)
        self.assertLess(listener_wait, holder_up)
        self.assertIn('grep -Fq "started with listener on :1935"', self.source)

    def test_local_images_are_built_before_holder_lifetime_starts(self) -> None:
        build = self.source.index(
            '"${compose[@]}" build continuity control-ui node-agent conflict-holder egress-conflict'
        )
        target_up = self.source.index(
            '"${compose[@]}" up -d egress-conflict-target', build
        )
        holder_up = self.source.index(
            '"${compose[@]}" up -d mediamtx continuity control-ui node-agent conflict-holder',
            target_up,
        )
        egress_up = self.source.index(
            '"${compose[@]}" up -d egress-conflict', holder_up
        )
        self.assertLess(build, target_up)
        self.assertLess(target_up, holder_up)
        self.assertLess(holder_up, egress_up)
        self.assertNotIn("--build", self.source[holder_up:egress_up])
        self.assertIn(
            "exec timeout --signal=INT --kill-after=5s 300s gst-launch-1.0",
            self.source,
        )


if __name__ == "__main__":
    unittest.main()
