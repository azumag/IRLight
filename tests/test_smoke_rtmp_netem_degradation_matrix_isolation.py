from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "smoke-rtmp-netem-degradation-matrix.sh"
CORE = ROOT / "scripts" / "smoke-rtmp-netem-degradation-matrix-core.sh"
LATENCY_HARNESS = ROOT / "scripts" / "smoke-netem-latency-levels.sh"
MATRIX_WORKFLOW = ROOT / ".github" / "workflows" / "rtmp-netem-degradation-matrix.yml"
BURST_WORKFLOW = ROOT / ".github" / "workflows" / "rtmp-netem-burst-loss.yml"
LATENCY_WORKFLOW = ROOT / ".github" / "workflows" / "netem-latency-levels.yml"


class RtmpNetemDegradationMatrixSmokeIsolationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SCRIPT.read_text(encoding="utf-8")
        cls.core_source = CORE.read_text(encoding="utf-8")
        cls.latency_source = LATENCY_HARNESS.read_text(encoding="utf-8")
        cls.matrix_workflow = MATRIX_WORKFLOW.read_text(encoding="utf-8")
        cls.burst_workflow = BURST_WORKFLOW.read_text(encoding="utf-8")
        cls.latency_workflow = LATENCY_WORKFLOW.read_text(encoding="utf-8")

    def test_public_wrapper_quarantines_all_core_output(self) -> None:
        self.assertIn("umask 077", self.source)
        self.assertIn(
            'tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/irlight-rtmp-netem-matrix-wrapper.XXXXXX")"',
            self.source,
        )
        self.assertIn('raw_log="$tmp_dir/core.log"', self.source)
        self.assertIn('bash "$core_script" >"$raw_log" 2>&1 &', self.source)
        self.assertNotIn('cat "$raw_log"', self.source)
        self.assertNotIn("ingest_secret", self.source)
        self.assertNotIn("session_json", self.source)
        self.assertIn("rtmp-netem-matrix-quarantined", self.source)

    def test_public_wrapper_forwards_signals_and_cleans_private_log(self) -> None:
        self.assertIn("trap cleanup_wrapper EXIT", self.source)
        self.assertIn("trap 'forward_signal 130' INT", self.source)
        self.assertIn("trap 'forward_signal 143' TERM", self.source)
        self.assertIn('kill -TERM "$core_pid"', self.source)
        self.assertIn('rm -rf "$tmp_dir"', self.source)

    def test_wrapper_only_emits_allowlisted_profile_results_after_core_success(self) -> None:
        self.assertIn('printf \'profile=%s result=PASS\\n\' "$profile"', self.source)
        self.assertIn("if (( status != 0 )); then", self.source)
        self.assertLess(
            self.source.index("if (( status != 0 )); then"),
            self.source.index("profile=%s result=PASS"),
        )
        for unsafe_name in (
            "publisher_log",
            "credential_secret",
            "session_events",
            "continuity status",
            "node-agent logs",
            "mediamtx logs",
        ):
            self.assertNotIn(unsafe_name, self.source)

    def test_burst_loss_proof_is_derived_privately_and_published_as_fixed_marker(self) -> None:
        self.assertIn('python3 - "$raw_log" "$loss_percent" "$loss_correlation"', self.source)
        self.assertIn("rtmp-netem-matrix-loss-correlation-proof-missing", self.source)
        self.assertIn("loss-correlation=%s verified=yes", self.source)
        self.assertIn(
            'grep -Fx "loss-correlation=${LOSS_CORRELATION} verified=yes"',
            self.burst_workflow,
        )
        self.assertNotIn('grep -Eq "loss 10', self.burst_workflow)

    def test_ci_entrypoints_do_not_bypass_public_wrapper(self) -> None:
        for workflow in (self.matrix_workflow, self.burst_workflow, self.latency_workflow):
            self.assertIn('"scripts/smoke-rtmp-netem-degradation-matrix-core.sh"', workflow)
        self.assertIn(
            "bash ./scripts/smoke-rtmp-netem-degradation-matrix.sh",
            self.matrix_workflow,
        )
        self.assertNotIn(
            "bash ./scripts/smoke-rtmp-netem-degradation-matrix-core.sh",
            self.matrix_workflow,
        )
        self.assertIn(
            "bash ./scripts/smoke-rtmp-netem-degradation-matrix.sh | tee",
            self.burst_workflow,
        )
        self.assertNotIn(
            "bash ./scripts/smoke-rtmp-netem-degradation-matrix-core.sh",
            self.burst_workflow,
        )

    def test_latency_harness_keeps_generated_rtmp_core_behind_wrapper(self) -> None:
        self.assertIn(
            'source_script="scripts/smoke-rtmp-netem-degradation-matrix-core.sh"',
            self.latency_source,
        )
        self.assertIn(
            'public_script="scripts/smoke-rtmp-netem-degradation-matrix.sh"',
            self.latency_source,
        )
        self.assertIn('RTMP_NETEM_MATRIX_CORE="$tmp_script"', self.latency_source)
        self.assertIn('bash "$public_script" | tee "$log_file"', self.latency_source)

    def test_compose_project_is_unique_per_run(self) -> None:
        self.assertIn(
            'smoke_project="irlight-rtmp-netem-matrix-smoke-$$-$RANDOM"',
            self.core_source,
        )
        expected = (
            'docker compose -p "$smoke_project" '
            '-f "$repo_root/docker-compose.poc.yml" -f "$override"'
        )
        self.assertIn(f"compose=({expected})", self.core_source)
        self.assertNotIn("COMPOSE_PROJECT_NAME", self.core_source)

    def test_cleanup_only_targets_generated_project(self) -> None:
        cleanup = self.core_source.split("cleanup() {", 1)[1].split("\n}\ntrap cleanup", 1)[0]
        self.assertIn('"${compose[@]}" down --volumes --remove-orphans', cleanup)
        self.assertNotIn("docker compose down", cleanup)
        self.assertNotIn("down -v", cleanup)

    def test_core_does_not_preemptively_stop_existing_stack(self) -> None:
        before_up = self.core_source.split('"${compose[@]}" up -d --build control-ui', 1)[0]
        after_trap = before_up.split("trap cleanup EXIT", 1)[1]
        self.assertNotIn('"${compose[@]}" down', after_trap)
        self.assertIn('"${compose[@]}" config >/dev/null', after_trap)

    def test_core_temporary_material_is_private_and_run_scoped(self) -> None:
        self.assertIn("umask 077", self.core_source)
        self.assertIn('tmp_dir="$(mktemp -d)"', self.core_source)
        self.assertIn('cookie_jar="$tmp_dir/cookies.txt"', self.core_source)
        self.assertIn('override="$tmp_dir/rtmp-netem-matrix.override.yml"', self.core_source)
        self.assertIn('publisher_log="$tmp_dir/publisher.log"', self.core_source)

    def test_netem_helper_is_resolved_from_repository_root(self) -> None:
        self.assertIn(
            'bash "$repo_root/scripts/netem-container.sh" apply "$publisher_name"',
            self.core_source,
        )
        self.assertIn(
            'bash "$repo_root/scripts/netem-container.sh" show "$publisher_name"',
            self.core_source,
        )
        self.assertIn(
            'bash "$repo_root/scripts/netem-container.sh" clear "$publisher_name"',
            self.core_source,
        )
        self.assertNotIn("bash ./scripts/netem-container.sh", self.core_source)

    def test_existing_matrix_behavior_contract_remains_in_core(self) -> None:
        for profile in (
            "loss-1",
            "loss-3",
            "loss-5",
            "loss-10",
            "latency-jitter",
            "bandwidth-800k",
        ):
            self.assertIn(profile, self.core_source)
        for contract in (
            "assert_session_nonterminal",
            "assert_profile_events_safe",
            "assert_output_relay_online",
            'kill -0 "$publisher_pid"',
            "wait_continuity_live",
        ):
            self.assertIn(contract, self.core_source)


if __name__ == "__main__":
    unittest.main()
