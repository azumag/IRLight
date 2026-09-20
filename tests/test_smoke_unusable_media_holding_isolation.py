from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "smoke-unusable-media-holding.sh"
CORE = ROOT / "scripts" / "smoke-unusable-media-holding-core.sh"
SUITE = ROOT / "scripts" / "ci-docker-smoke-suite.sh"


class UnusableMediaHoldingSmokeIsolationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SCRIPT.read_text(encoding="utf-8")
        cls.core_source = CORE.read_text(encoding="utf-8")
        cls.suite_source = SUITE.read_text(encoding="utf-8")

    def test_public_wrapper_quarantines_all_core_output(self) -> None:
        self.assertIn("umask 077", self.source)
        self.assertIn(
            'tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/irlight-unusable-media-holding-wrapper.XXXXXX")"',
            self.source,
        )
        self.assertIn('raw_log="$tmp_dir/unusable-media-holding.raw.log"', self.source)
        self.assertIn(
            'bash "$script_dir/smoke-unusable-media-holding-core.sh" >"$raw_log" 2>&1 &',
            self.source,
        )
        self.assertNotIn('cat "$raw_log"', self.source)
        self.assertNotIn('tail "$raw_log"', self.source)
        self.assertIn("unusable-media-holding-quarantined", self.source)

        error_line = next(
            line
            for line in self.source.splitlines()
            if "unusable-media-holding-quarantined" in line
        )
        for runtime_secret in (
            "$ingest_username",
            "$ingest_secret",
            "$cookie_jar",
            "$csrf",
            "$publisher_log",
            "$invalid_publisher_log",
            "$raw_log",
        ):
            self.assertNotIn(runtime_secret, error_line)

    def test_public_wrapper_forwards_signals_and_cleans_private_log(self) -> None:
        self.assertIn("trap cleanup_wrapper EXIT", self.source)
        self.assertIn("trap 'forward_signal 130' INT", self.source)
        self.assertIn("trap 'forward_signal 143' TERM", self.source)
        self.assertIn('kill -TERM "$core_pid"', self.source)
        self.assertIn('rm -rf "$tmp_dir"', self.source)

    def test_ci_suite_uses_only_public_wrapper(self) -> None:
        self.assertIn("scripts/smoke-unusable-media-holding.sh", self.suite_source)
        self.assertNotIn("scripts/smoke-unusable-media-holding-core.sh", self.suite_source)

    def test_core_compose_project_is_unique_per_run(self) -> None:
        self.assertIn(
            'smoke_project="irlight-unusable-media-holding-smoke-$$-$RANDOM"',
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
        self.assertIn('override="$tmp_dir/unusable-media.override.yml"', self.core_source)
        self.assertIn('publisher_log="$tmp_dir/publisher.log"', self.core_source)
        self.assertIn(
            'invalid_publisher_log="$tmp_dir/invalid-publisher.log"',
            self.core_source,
        )
        self.assertNotIn(
            ">/tmp/irlight-unusable-media-publisher.log",
            self.core_source,
        )
        self.assertNotIn(
            ">/tmp/irlight-format-change-publisher.log",
            self.core_source,
        )

    def test_unusable_media_contract_remains_explicit_in_core(self) -> None:
        for marker in (
            "wait_session_status LIVE 50",
            "wait_holding_reason VIDEO_TIMEOUT 45",
            "wait_holding_reason AUDIO_TIMEOUT 45",
            "wait_mediamtx_resolution 640 360 20",
            "wait_holding_reason FORMAT_CHANGED 45",
            'kill -0 "$publisher_pid"',
            'reason == "VIDEO_TIMEOUT"',
            'reason == "AUDIO_TIMEOUT"',
            'reason == "FORMAT_CHANGED" and "RESOLUTION_UNSUPPORTED" in reasons',
            'grep -Fq "$ingest_secret" <<<"$final_events"',
        ):
            self.assertIn(marker, self.core_source)


if __name__ == "__main__":
    unittest.main()
