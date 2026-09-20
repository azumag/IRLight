from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "smoke-ingest-disconnect-recovery.sh"


class IngestDisconnectRecoverySmokeIsolationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SCRIPT.read_text(encoding="utf-8")

    def test_compose_project_is_unique_per_run(self) -> None:
        self.assertIn(
            'smoke_project="irlight-ingest-disconnect-recovery-smoke-$$-$RANDOM"',
            self.source,
        )
        expected = (
            'docker compose -p "$smoke_project" '
            '-f "$repo_root/docker-compose.poc.yml" -f "$override"'
        )
        self.assertIn(f"compose=({expected})", self.source)
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
        self.assertIn('override="$tmp_dir/disconnect-recovery.override.yml"', self.source)
        self.assertIn('publisher_log="$tmp_dir/publisher.log"', self.source)
        self.assertNotIn(
            ">/tmp/irlight-disconnect-recovery-publisher.log",
            self.source,
        )

    def test_failure_cleanup_quarantines_credential_bearing_logs(self) -> None:
        cleanup = self.source.split("cleanup() {", 1)[1].split("\n}\ntrap cleanup", 1)[0]
        self.assertIn("quarantine_failure_diagnostics", cleanup)
        self.assertNotIn('"${compose[@]}" logs', cleanup)
        self.assertNotIn('cat "$publisher_log"', cleanup)

        quarantine = self.source.split("quarantine_failure_diagnostics() {", 1)[1].split(
            "\n}\n\ncleanup()",
            1,
        )[0]
        self.assertIn("Failure diagnostics quarantined", quarantine)
        for runtime_secret in (
            "$ingest_username",
            "$ingest_secret",
            "$cookie_jar",
            "$csrf",
            "$publisher_log",
        ):
            self.assertNotIn(runtime_secret, quarantine)

    def test_timeout_paths_do_not_emit_raw_api_payloads(self) -> None:
        wait_session = self.source.split("wait_session_status() {", 1)[1].split(
            "\n}\n\nwait_holding_reason()",
            1,
        )[0]
        self.assertIn("Session and event timeout diagnostics quarantined.", wait_session)
        self.assertNotIn("session_json >&2", wait_session)
        self.assertNotIn("session_events >&2", wait_session)

        wait_holding = self.source.split("wait_holding_reason() {", 1)[1].split(
            "\n}\n\nwait_assigned_node()",
            1,
        )[0]
        self.assertIn("Session and event timeout diagnostics quarantined.", wait_holding)
        self.assertNotIn("session_json >&2", wait_holding)
        self.assertNotIn("session_events >&2", wait_holding)

        wait_node = self.source.split("wait_assigned_node() {", 1)[1].split(
            "\n}\n\nwait_recovery_candidate()",
            1,
        )[0]
        self.assertIn("Node assignment timeout diagnostics quarantined.", wait_node)
        self.assertNotIn('node_admin_curl -fsS "$base_url/internal/nodes" >&2', wait_node)

        wait_candidate = self.source.split("wait_recovery_candidate() {", 1)[1].split(
            "\n}\n\nstop_publisher()",
            1,
        )[0]
        self.assertIn("Session and event timeout diagnostics quarantined.", wait_candidate)
        self.assertNotIn("session_json >&2", wait_candidate)
        self.assertNotIn("session_events >&2", wait_candidate)

    def test_disconnect_recovery_contract_remains_explicit(self) -> None:
        for marker in (
            "wait_session_status LIVE 60",
            "wait_holding_reason INGEST_DISCONNECTED 60",
            'candidate_info="$(wait_recovery_candidate 45)"',
            "wait_session_status LIVE 75",
            'kill -0 "$publisher_pid"',
            'e.get("type") == "ingest.reconnected"',
            'e.get("type") == "session.recovered"',
            "minimum = max(0.0, stable - 0.5)",
        ):
            self.assertIn(marker, self.source)


if __name__ == "__main__":
    unittest.main()
