from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "smoke-short-media-stall.sh"
CORE = ROOT / "scripts" / "smoke-short-media-stall-core.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "short-media-stall.yml"


class ShortMediaStallSmokeIsolationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SCRIPT.read_text(encoding="utf-8")
        cls.core_source = CORE.read_text(encoding="utf-8")
        cls.workflow_source = WORKFLOW.read_text(encoding="utf-8")

    def test_public_wrapper_quarantines_all_core_output(self) -> None:
        self.assertIn("umask 077", self.source)
        self.assertIn(
            'tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/irlight-short-media-stall-wrapper.XXXXXX")"',
            self.source,
        )
        self.assertIn('raw_log="$tmp_dir/short-media-stall.raw.log"', self.source)
        self.assertIn(
            'bash "$script_dir/smoke-short-media-stall-core.sh" >"$raw_log" 2>&1 &',
            self.source,
        )
        self.assertNotIn('cat "$raw_log"', self.source)
        self.assertNotIn('tail "$raw_log"', self.source)
        self.assertIn("short-media-stall-quarantined", self.source)

        error_line = next(
            line
            for line in self.source.splitlines()
            if "short-media-stall-quarantined" in line
        )
        for runtime_secret in (
            "$ingest_username",
            "$ingest_secret",
            "$cookie_jar",
            "$csrf",
            "$publisher_log",
            "$raw_log",
        ):
            self.assertNotIn(runtime_secret, error_line)

    def test_public_wrapper_forwards_signals_and_cleans_private_log(self) -> None:
        self.assertIn("trap cleanup_wrapper EXIT", self.source)
        self.assertIn("trap 'forward_signal 130' INT", self.source)
        self.assertIn("trap 'forward_signal 143' TERM", self.source)
        self.assertIn('kill -TERM "$core_pid"', self.source)
        self.assertIn('rm -rf "$tmp_dir"', self.source)

    def test_workflow_uses_only_public_wrapper_and_tracks_core_changes(self) -> None:
        self.assertIn('- "scripts/smoke-short-media-stall.sh"', self.workflow_source)
        self.assertIn('- "scripts/smoke-short-media-stall-core.sh"', self.workflow_source)
        self.assertIn(
            "run: bash ./scripts/smoke-short-media-stall.sh",
            self.workflow_source,
        )
        self.assertNotIn(
            "run: bash ./scripts/smoke-short-media-stall-core.sh",
            self.workflow_source,
        )

    def test_core_compose_project_is_unique_per_run(self) -> None:
        self.assertIn(
            'smoke_project="irlight-short-media-stall-smoke-$$-$RANDOM"',
            self.core_source,
        )
        expected = (
            'docker compose -p "$smoke_project" '
            '-f "$repo_root/docker-compose.poc.yml" -f "$override"'
        )
        self.assertIn(f"compose=({expected})", self.core_source)
        self.assertNotIn("COMPOSE_PROJECT_NAME", self.core_source)

    def test_core_cleanup_only_targets_generated_project(self) -> None:
        cleanup = self.core_source.split("cleanup() {", 1)[1].split(
            "\n}\ntrap cleanup", 1
        )[0]
        self.assertIn('"${compose[@]}" down --volumes --remove-orphans', cleanup)
        self.assertNotIn("docker compose down", cleanup)
        self.assertNotIn("down -v", cleanup)

    def test_core_does_not_preemptively_stop_existing_stack(self) -> None:
        before_up = self.core_source.split(
            '"${compose[@]}" up -d --build control-ui', 1
        )[0]
        after_trap = before_up.split("trap cleanup EXIT", 1)[1]
        self.assertNotIn('"${compose[@]}" down', after_trap)
        self.assertIn('"${compose[@]}" config >/dev/null', after_trap)

    def test_core_temporary_material_is_private_and_run_scoped(self) -> None:
        self.assertIn("umask 077", self.core_source)
        self.assertIn('tmp_dir="$(mktemp -d)"', self.core_source)
        self.assertIn('cookie_jar="$tmp_dir/cookies.txt"', self.core_source)
        self.assertIn(
            'override="$tmp_dir/short-media-stall.override.yml"',
            self.core_source,
        )
        self.assertIn('publisher_log="$tmp_dir/publisher.log"', self.core_source)
        self.assertNotIn(
            ">/tmp/irlight-short-media-stall-publisher.log",
            self.core_source,
        )

    def test_media_stall_contract_remains_explicit_in_core(self) -> None:
        for marker in (
            "wait_session_status LIVE 60",
            'wait_stall_holding_after "$baseline_sequence" 45',
            "wait_continuity_state HOLDING STANDBY SILENT_FALLBACK 45",
            "assert_output_relay_online",
            'kill -0 "$publisher_pid"',
            'e.get("type") == "ingest.recovered"',
            'e.get("type") == "session.recovered"',
            "wait_continuity_state LIVE LIVE LIVE 60",
        ):
            self.assertIn(marker, self.core_source)


if __name__ == "__main__":
    unittest.main()
