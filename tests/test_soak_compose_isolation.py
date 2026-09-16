from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "soak-compose.sh"


class ComposeSoakIsolationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SCRIPT.read_text(encoding="utf-8")

    def test_compose_project_is_unique_per_run(self) -> None:
        self.assertIn('soak_project="irlight-poc-soak-$$-$RANDOM"', self.source)
        self.assertIn(
            'compose=(docker compose -p "$soak_project" -f "$repo_root/docker-compose.poc.yml")',
            self.source,
        )
        self.assertNotIn("IRLIGHT_SOAK_PROJECT", self.source)
        self.assertNotIn('COMPOSE_PROJECT_NAME:-', self.source)

    def test_cleanup_only_targets_generated_project(self) -> None:
        cleanup = self.source.split("cleanup() {", 1)[1].split("\n}\ntrap cleanup", 1)[0]
        self.assertIn(
            '"${compose[@]}" down --rmi local --volumes --remove-orphans',
            cleanup,
        )
        self.assertNotIn("docker compose down", cleanup)
        self.assertIn(
            'python3 "$repo_root/scripts/verify-soak-cleanup.py" --project "$soak_project"',
            cleanup,
        )

    def test_cleanup_failure_cannot_turn_a_successful_soak_green(self) -> None:
        cleanup = self.source.split("cleanup() {", 1)[1].split("\n}\ntrap cleanup", 1)[0]
        self.assertIn("local cleanup_status=0", cleanup)
        self.assertGreaterEqual(cleanup.count("cleanup_status=1"), 2)
        self.assertIn('if (( status != 0 )); then\n    exit "$status"', cleanup)
        self.assertIn('exit "$cleanup_status"', cleanup)
        self.assertIn("trap - EXIT", cleanup)

    def test_script_does_not_preemptively_stop_existing_stack(self) -> None:
        before_up = self.source.split('"${compose[@]}" up -d --build', 1)[0]
        after_trap = before_up.split("trap cleanup EXIT", 1)[1]
        self.assertNotIn('"${compose[@]}" down', after_trap)
        self.assertIn('"${compose[@]}" config >/dev/null', after_trap)

    def test_compose_file_is_resolved_from_repository_root(self) -> None:
        self.assertIn(
            'repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"',
            self.source,
        )
        self.assertIn('-f "$repo_root/docker-compose.poc.yml"', self.source)

    def test_evidence_orchestrator_uses_owned_project_and_same_timing(self) -> None:
        start = self.source.split("start_evidence_collector() {", 1)[1].split(
            "\n}\n\nwait_evidence_collector()", 1
        )[0]
        self.assertIn('python3 "$repo_root/scripts/orchestrate-soak-samples.py"', start)
        self.assertIn('--project "$soak_project"', start)
        self.assertIn('--compose-file "$repo_root/docker-compose.poc.yml"', start)
        self.assertIn('--duration-seconds "$soak_seconds"', start)
        self.assertIn('--interval-seconds "$interval_seconds"', start)
        self.assertIn('--samples-jsonl "$samples_jsonl"', start)
        self.assertIn('evidence_pid=$!', start)

    def test_evidence_requires_measured_media_or_explicit_diagnostic_mode(self) -> None:
        self.assertIn('samples_jsonl="${SOAK_SAMPLES_JSONL:-}"', self.source)
        self.assertIn('media_metrics_file="${SOAK_MEDIA_METRICS_FILE:-}"', self.source)
        self.assertIn('allow_unmeasured_media="${SOAK_ALLOW_UNMEASURED_MEDIA:-0}"', self.source)
        self.assertIn(
            'SOAK_SAMPLES_JSONL requires SOAK_MEDIA_METRICS_FILE or '
            'SOAK_ALLOW_UNMEASURED_MEDIA=1',
            self.source,
        )
        self.assertIn(
            'SOAK_MEDIA_METRICS_FILE and SOAK_ALLOW_UNMEASURED_MEDIA=1 are mutually exclusive',
            self.source,
        )
        self.assertEqual(self.source.count("evidence+=(--allow-unmeasured-media)"), 1)

    def test_cleanup_stops_evidence_before_compose_teardown(self) -> None:
        cleanup = self.source.split("cleanup() {", 1)[1].split("\n}\ntrap cleanup", 1)[0]
        self.assertIn("stop_evidence_collector", cleanup)
        self.assertLess(
            cleanup.index("stop_evidence_collector"),
            cleanup.index('"${compose[@]}" down --rmi local --volumes --remove-orphans'),
        )

    def test_evidence_failure_prevents_success(self) -> None:
        tail = self.source.split("start_evidence_collector\n", 1)[1]
        self.assertIn("wait_evidence_collector", tail)
        self.assertLess(
            tail.index("wait_evidence_collector"),
            tail.index('echo "IRLight compose soak passed:'),
        )
        wait_fn = self.source.split("wait_evidence_collector() {", 1)[1].split(
            '\n}\n\n"${compose[@]}" config', 1
        )[0]
        self.assertIn('echo "soak evidence collection failed" >&2', wait_fn)
        self.assertIn('kill -TERM "$pid"', wait_fn)
        self.assertIn('wait "$pid" 2>/dev/null || true', wait_fn)


if __name__ == "__main__":
    unittest.main()
