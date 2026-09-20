from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "smoke-session-ingest-events.sh"


class SessionIngestEventsSmokeIsolationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SCRIPT.read_text(encoding="utf-8")

    def test_compose_project_is_unique_per_run(self) -> None:
        self.assertIn(
            'smoke_project="irlight-session-events-smoke-$$-$RANDOM"',
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
        self.assertIn('cookie_jar="$tmp_dir/cookies.txt"', self.source)
        self.assertIn('publisher_log="$tmp_dir/publisher.log"', self.source)
        self.assertIn('>"$publisher_log" 2>&1 &', self.source)
        self.assertNotIn("/tmp/irlight-session-event-publisher.log", self.source)

    def test_failure_cleanup_never_replays_secret_bearing_logs(self) -> None:
        cleanup = self.source.split("cleanup() {", 1)[1].split("\n}\ntrap cleanup", 1)[0]
        self.assertIn('withhold_sensitive_diagnostics "node-agent/service"', cleanup)
        self.assertIn('withhold_sensitive_diagnostics "control-ui/service"', cleanup)
        self.assertIn('withhold_sensitive_diagnostics "mediamtx/service"', cleanup)
        self.assertIn('withhold_sensitive_diagnostics "publisher"', cleanup)
        self.assertNotIn("logs --no-color", cleanup)
        self.assertNotIn('tail -100 "$publisher_log"', cleanup)
        self.assertNotIn('cat "$publisher_log"', cleanup)

    def test_timeout_paths_do_not_replay_api_payloads(self) -> None:
        status_wait = self.source.split("wait_session_status() {", 1)[1].split(
            "\n}\n\nwait_session_event() {", 1
        )[0]
        self.assertIn('withhold_sensitive_diagnostics "Session API"', status_wait)
        self.assertNotIn("session_json >&2", status_wait)
        self.assertNotIn('echo "$payload"', status_wait)

        event_wait = self.source.split("wait_session_event() {", 1)[1].split(
            "\n}\n\nwait_assigned_node() {", 1
        )[0]
        self.assertIn('withhold_sensitive_diagnostics "Session event API"', event_wait)
        self.assertNotIn('echo "$payload"', event_wait)

        node_wait = self.source.split("wait_assigned_node() {", 1)[1].split(
            "\n}\n\nstart_publisher() {", 1
        )[0]
        self.assertIn('withhold_sensitive_diagnostics "Node API"', node_wait)
        self.assertNotIn("node_admin_curl -fsS \"$base_url/internal/nodes\" >&2", node_wait)
        self.assertNotIn('echo "$payload"', node_wait)

    def test_quarantine_message_contains_no_runtime_secret_values(self) -> None:
        helper = self.source.split("withhold_sensitive_diagnostics() {", 1)[1].split(
            "\n}\n\ncleanup() {", 1
        )[0]
        self.assertNotIn("$password", helper)
        self.assertNotIn("$csrf", helper)
        self.assertNotIn("$ingest_username", helper)
        self.assertNotIn("$ingest_secret", helper)
        self.assertNotIn("$bootstrap_token", helper)
        self.assertNotIn("$cookie_jar", helper)

    def test_api_assertions_never_embed_raw_payloads_in_failures(self) -> None:
        self.assertNotIn("assert item.get(\"node_id\"), item", self.source)
        self.assertNotIn("assert item.get(\"first_ingest_at\") is not None, item", self.source)
        self.assertNotIn("assert required.issubset({e.get(\"type\") for e in events}), events", self.source)
        self.assertNotIn("assert forbidden.isdisjoint(payload), event", self.source)
        self.assertIn(
            'raise SystemExit("Session event payload contains a forbidden credential field")',
            self.source,
        )
        self.assertIn(
            'raise SystemExit("required Session ingest events are missing")',
            self.source,
        )

    def test_session_ingest_event_contract_remains_intact(self) -> None:
        self.assertIn('if [[ "$auth_status" != "401" ]]', self.source)
        self.assertIn("wait_session_event ingest.auth_failed 15", self.source)
        self.assertIn("wait_session_status LIVE 45", self.source)
        self.assertIn("wait_session_event ingest.connected 30", self.source)
        self.assertIn("wait_session_event ingest.format_detected 30", self.source)
        self.assertIn("wait_session_status HOLDING 30", self.source)
        self.assertIn("wait_session_event ingest.disconnected 30", self.source)
        self.assertIn('if grep -Fq "$ingest_secret" <<<"$events"', self.source)


if __name__ == "__main__":
    unittest.main()
