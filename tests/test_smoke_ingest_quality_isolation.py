from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "smoke-ingest-quality.sh"


class IngestQualitySmokeIsolationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SCRIPT.read_text(encoding="utf-8")

    def test_compose_project_is_unique_per_run(self) -> None:
        self.assertIn(
            'smoke_project="irlight-ingest-quality-smoke-$$-$RANDOM"',
            self.source,
        )
        self.assertIn(
            'compose=(docker compose -p "$smoke_project" '
            '-f "$repo_root/docker-compose.poc.yml" -f "$override")',
            self.source,
        )
        self.assertNotIn("COMPOSE_PROJECT_NAME", self.source)

    def test_cleanup_only_targets_generated_project(self) -> None:
        cleanup = self.source.split("cleanup() {", 1)[1].split("\n}\ntrap cleanup", 1)[0]
        self.assertIn('"${compose[@]}" down --volumes --remove-orphans', cleanup)
        self.assertNotIn("docker compose down", cleanup)
        self.assertNotIn("down -v", cleanup)

    def test_script_does_not_preemptively_stop_existing_stack(self) -> None:
        before_up = self.source.split('"${compose[@]}" up -d --build control-ui', 1)[0]
        after_trap = before_up.split("trap cleanup EXIT", 1)[1]
        self.assertNotIn('"${compose[@]}" down', after_trap)
        self.assertIn('"${compose[@]}" config >/dev/null', after_trap)

    def test_temporary_material_is_private_and_run_scoped(self) -> None:
        self.assertIn("umask 077", self.source)
        self.assertIn('tmp_dir="$(mktemp -d)"', self.source)
        self.assertIn('cookie_jar="$tmp_dir/quality-cookies.txt"', self.source)
        self.assertIn('publisher_log="$tmp_dir/quality-publisher.log"', self.source)
        self.assertNotIn("/tmp/irlight-quality-", self.source)

    def test_failure_cleanup_does_not_replay_service_or_publisher_logs(self) -> None:
        cleanup = self.source.split("cleanup() {", 1)[1].split("\n}\ntrap cleanup", 1)[0]
        self.assertIn('withhold_sensitive_diagnostics "node-agent/service"', cleanup)
        self.assertIn('withhold_sensitive_diagnostics "mediamtx/service"', cleanup)
        self.assertIn('withhold_sensitive_diagnostics "control-ui/service"', cleanup)
        self.assertIn('withhold_sensitive_diagnostics "publisher"', cleanup)
        self.assertNotIn("logs --no-color", cleanup)
        self.assertNotIn('tail -120 "$publisher_log"', cleanup)
        self.assertNotIn('cat "$publisher_log"', cleanup)

    def test_node_timeout_paths_do_not_replay_api_payloads(self) -> None:
        assigned_wait = self.source.split("wait_assigned_node() {", 1)[1].split(
            "\n}\n\nwait_node_status() {", 1
        )[0]
        self.assertIn('withhold_sensitive_diagnostics "Node assignment API"', assigned_wait)
        self.assertNotIn('echo "$payload"', assigned_wait)

        status_wait = self.source.split("wait_node_status() {", 1)[1].split(
            "\n}\n\nwait_node_event() {", 1
        )[0]
        self.assertIn('withhold_sensitive_diagnostics "Node status API"', status_wait)
        self.assertNotIn('node_admin_curl -fsS "$base_url/internal/nodes" >&2', status_wait)
        self.assertNotIn('echo "$payload"', status_wait)

        event_wait = self.source.split("wait_node_event() {", 1)[1].split(
            "\n}\n\nsetup_ingest() {", 1
        )[0]
        self.assertIn('withhold_sensitive_diagnostics "Node event API"', event_wait)
        self.assertNotIn('echo "$payload"', event_wait)

    def test_quarantine_message_contains_no_runtime_secret_values(self) -> None:
        helper = self.source.split("withhold_sensitive_diagnostics() {", 1)[1].split(
            "\n}\n\ncleanup() {", 1
        )[0]
        self.assertNotIn("$ingest_username", helper)
        self.assertNotIn("$ingest_secret", helper)
        self.assertNotIn("$cookie_jar", helper)
        self.assertNotIn("$publisher_log", helper)

    def test_quality_contract_remains_intact(self) -> None:
        self.assertIn("start_publisher 10 18", self.source)
        self.assertIn("wait_node_status DEGRADED FPS_OUT_OF_RANGE 45", self.source)
        self.assertIn("wait_node_event ingest.degraded 20", self.source)
        self.assertIn("wait_node_status OFFLINE \"\" 25", self.source)
        self.assertIn("start_publisher 30 30", self.source)
        self.assertIn("wait_node_status ACCEPTED \"\" 45", self.source)
        self.assertGreaterEqual(self.source.count('kill -0 "$publisher_pid"'), 2)


if __name__ == "__main__":
    unittest.main()
