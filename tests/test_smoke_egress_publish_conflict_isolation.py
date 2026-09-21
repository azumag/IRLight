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

    def test_temporary_secret_material_is_private_and_shell_values_are_dropped(self) -> None:
        self.assertIn("umask 077", self.source)
        self.assertIn('tmp_dir="$(mktemp -d)"', self.source)
        self.assertIn('secret_file="$tmp_dir/egress_url"', self.source)
        self.assertIn('redaction_values_file="$tmp_dir/redaction-values"', self.source)
        self.assertIn('printf \'%s\\n\' "$stream_key" >"$redaction_values_file"', self.source)
        self.assertIn(
            'chmod 600 "$secret_file" "$redaction_values_file" "$target_config"',
            self.source,
        )
        self.assertIn('chmod 600 "$override"', self.source)
        self.assertIn("unset stream_key path_name", self.source)

        post_unset = self.source.split("unset stream_key path_name", 1)[1]
        self.assertNotIn("$stream_key", post_unset)
        self.assertNotIn("$path_name", post_unset)

    def test_failure_diagnostics_read_secret_only_from_private_file(self) -> None:
        self.assertIn("redact_generated_secrets() {", self.source)
        redactor = self.source.split("redact_generated_secrets() {", 1)[1].split(
            "\n}\n\nstdin_excludes_generated_secrets() {", 1
        )[0]
        self.assertIn("Path(sys.argv[1]).read_bytes()", redactor)
        self.assertIn("if not secrets:", redactor)
        self.assertIn("data.replace(secret, b\"<redacted>\")", redactor)
        self.assertIn("' \"$redaction_values_file\"", redactor)

        helper = self.source.split("emit_redacted_compose_logs() {", 1)[1].split(
            "\n}\n\ncapture_compose_logs() {", 1
        )[0]
        self.assertIn('>"$raw_file" 2>&1 || logs_rc=$?', helper)
        self.assertIn('redact_generated_secrets <"$raw_file" >&2', helper)
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

    def test_status_payload_is_parsed_from_stdin_not_helper_argv(self) -> None:
        matcher = self.source.split("status_matches_reason() {", 1)[1].split(
            "\n}\n\nemit_redacted_compose_logs() {", 1
        )[0]
        self.assertIn("value=json.load(sys.stdin)", matcher)
        self.assertIn("sys.argv[1]", matcher)
        self.assertIn("sys.argv[2]", matcher)

        wait = self.source.split("wait_status_reason() {", 1)[1].split(
            "\n}\n\nwait_for_target_listener() {", 1
        )[0]
        self.assertIn(
            'status_matches_reason "$expected_status" "$expected_reason" <<<"$payload"',
            wait,
        )
        self.assertNotIn('json.loads(sys.argv[1])', wait)
        self.assertNotIn('"$payload" "$expected_status"', wait)

    def test_timeout_status_diagnostics_fail_closed_on_redaction(self) -> None:
        wait = self.source.split("wait_status_reason() {", 1)[1].split(
            "\n}\n\nwait_for_target_listener() {", 1
        )[0]
        self.assertIn(
            'safe_payload="$(printf \'%s\' "$payload" | redact_generated_secrets 2>/dev/null)"',
            wait,
        )
        self.assertIn("last=$safe_payload", wait)
        self.assertIn("last=<redaction-failed>", wait)
        self.assertNotIn("last=$payload", wait)

    def test_secret_assertions_use_private_secret_file_and_fail_closed(self) -> None:
        stdin_helper = self.source.split("stdin_excludes_generated_secrets() {", 1)[1].split(
            "\n}\n\nfile_excludes_generated_secrets() {", 1
        )[0]
        self.assertIn("Path(sys.argv[1]).read_bytes()", stdin_helper)
        self.assertIn("secrets and all(secret not in data", stdin_helper)

        file_helper = self.source.split("file_excludes_generated_secrets() {", 1)[1].split(
            "\n}\n\nstatus_matches_reason() {", 1
        )[0]
        self.assertIn("Path(sys.argv[1]).read_bytes()", file_helper)
        self.assertIn("Path(sys.argv[2]).read_bytes()", file_helper)
        self.assertIn("secrets and all(secret not in data", file_helper)

        final_checks = self.source.split('status_payload="$(read_status)"', 1)[1]
        self.assertIn('if [[ -z "$status_payload" ]]; then', final_checks)
        self.assertIn(
            'stdin_excludes_generated_secrets <<<"$status_payload"', final_checks
        )
        self.assertIn(
            'egress_logs_file="$tmp_dir/egress-conflict.log"', final_checks
        )
        self.assertIn(
            '"${compose[@]}" logs --no-color egress-conflict >"$egress_logs_file" 2>&1',
            final_checks,
        )
        self.assertIn(
            'file_excludes_generated_secrets "$egress_logs_file"', final_checks
        )
        self.assertNotIn('grep -Fq "$stream_key"', final_checks)

    def test_secret_derived_target_markers_are_reconstructed_inside_helper(self) -> None:
        marker = self.source.split("target_logs_contain_path_marker() {", 1)[1].split(
            "\n}\n\ncleanup() {", 1
        )[0]
        self.assertIn("Path(sys.argv[1]).read_bytes()", marker)
        self.assertIn('path = b"conflict/" + secrets[0]', marker)
        self.assertIn('"publishing": b"is publishing to path', marker)
        self.assertIn('"conflict": b"someone is already publishing to path', marker)
        self.assertIn('"$redaction_values_file" "$marker_kind" "$output_file"', marker)
        self.assertIn("raise SystemExit(2)", marker)

        holder_poll = self.source.split(
            'holder_poll_logs="$tmp_dir/egress-conflict-target.holder-poll.log"', 1
        )[1].split(
            'holder_final_logs="$tmp_dir/egress-conflict-target.holder-final.log"', 1
        )[0]
        self.assertIn(
            'target_logs_contain_path_marker publishing "$holder_poll_logs"',
            holder_poll,
        )

        conflict_final = self.source.split(
            'conflict_evidence_logs="$tmp_dir/egress-conflict-target.conflict-evidence.log"',
            1,
        )[1].split("# The rejection is terminal", 1)[0]
        self.assertIn(
            'target_logs_contain_path_marker conflict "$conflict_evidence_logs"',
            conflict_final,
        )
        self.assertNotIn("$path_name", holder_poll)
        self.assertNotIn("$path_name", conflict_final)

    def test_nonsecret_log_marker_probe_still_captures_complete_logs(self) -> None:
        capture = self.source.split("capture_compose_logs() {", 1)[1].split(
            "\n}\n\ncompose_logs_contain_marker() {", 1
        )[0]
        self.assertIn(
            '"${compose[@]}" logs --no-color "$service" >"$output_file" 2>&1',
            capture,
        )
        marker = self.source.split("compose_logs_contain_marker() {", 1)[1].split(
            "\n}\n\ntarget_logs_contain_path_marker() {", 1
        )[0]
        self.assertIn('capture_compose_logs "$service" "$output_file"', marker)
        self.assertIn('grep -Fq -- "$marker" "$output_file"', marker)
        self.assertNotIn(
            'logs --no-color egress-conflict-target 2>/dev/null | grep -Fq',
            self.source,
        )

    def test_polling_keeps_existing_timeout_and_liveness_contracts(self) -> None:
        listener = self.source.split("wait_for_target_listener() {", 1)[1].split(
            "\n}\n\n\"${compose[@]}\" config", 1
        )[0]
        self.assertIn('local timeout="${1:-30}"', listener)
        self.assertIn(
            'compose_logs_contain_marker egress-conflict-target "started with listener on :1935"',
            listener,
        )
        self.assertIn(
            '"${compose[@]}" ps --status running --services | grep -qx egress-conflict-target',
            listener,
        )
        self.assertIn("sleep 1", listener)

        holder_poll = self.source.split(
            'holder_poll_logs="$tmp_dir/egress-conflict-target.holder-poll.log"', 1
        )[1].split(
            'holder_final_logs="$tmp_dir/egress-conflict-target.holder-final.log"', 1
        )[0]
        self.assertIn("for _ in $(seq 1 20); do", holder_poll)
        self.assertIn(
            'target_logs_contain_path_marker publishing "$holder_poll_logs"',
            holder_poll,
        )
        self.assertIn("sleep 1", holder_poll)

    def test_final_target_evidence_fails_closed_on_log_or_secret_read_error(self) -> None:
        holder_final = self.source.split(
            'holder_final_logs="$tmp_dir/egress-conflict-target.holder-final.log"', 1
        )[1].split('"${compose[@]}" up -d egress-conflict', 1)[0]
        self.assertIn("marker_rc=$?", holder_final)
        self.assertIn("if (( marker_rc == 2 )); then", holder_final)
        self.assertIn(
            "failed to read target logs while confirming first publisher", holder_final
        )

        conflict_final = self.source.split(
            'conflict_evidence_logs="$tmp_dir/egress-conflict-target.conflict-evidence.log"',
            1,
        )[1].split("# The rejection is terminal", 1)[0]
        self.assertIn("marker_rc=$?", conflict_final)
        self.assertIn("if (( marker_rc == 2 )); then", conflict_final)
        self.assertIn(
            "failed to read target logs while confirming publish conflict",
            conflict_final,
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
        self.assertIn(
            'compose_logs_contain_marker egress-conflict-target "started with listener on :1935"',
            self.source,
        )

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
