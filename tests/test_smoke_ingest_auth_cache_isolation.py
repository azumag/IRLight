from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "smoke-ingest-auth-cache.sh"


class IngestAuthCacheSmokeIsolationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SCRIPT.read_text(encoding="utf-8")

    def test_compose_project_is_per_run_and_not_user_supplied(self) -> None:
        self.assertIn(
            'smoke_project="irlight-ingest-auth-cache-smoke-$$-$RANDOM"',
            self.source,
        )
        self.assertIn(
            'compose=(docker compose -p "$smoke_project" '
            '-f docker-compose.poc.yml -f "$override")',
            self.source,
        )
        self.assertNotIn("COMPOSE_PROJECT_NAME", self.source.split("compose=(", 1)[0])
        self.assertNotIn("IRLIGHT_SMOKE_PROJECT", self.source)

    def test_cleanup_only_targets_generated_project(self) -> None:
        cleanup = self.source.split("cleanup() {", 1)[1].split("\n}\ntrap cleanup", 1)[0]
        self.assertIn('"${compose[@]}" down --volumes --remove-orphans', cleanup)
        self.assertNotIn("docker compose down", cleanup)
        self.assertNotIn("down -v", cleanup)

    def test_sensitive_runtime_files_live_under_private_temp_dir(self) -> None:
        self.assertIn("umask 077", self.source)
        self.assertIn('cookie_jar="$tmp_dir/cookies.txt"', self.source)
        self.assertIn('publisher_log="$tmp_dir/publisher.log"', self.source)
        self.assertIn(
            'redaction_values="$tmp_dir/ingest-auth-cache.redaction-values"',
            self.source,
        )
        self.assertIn('>"$publisher_log" 2>&1 &', self.source)
        self.assertNotIn("/tmp/irlight-cache-smoke-cookies.txt", self.source)
        self.assertNotIn("/tmp/irlight-cache-publisher.log", self.source)

    def test_failure_diagnostics_redact_credentials_without_raw_fallback(self) -> None:
        cleanup = self.source.split("cleanup() {", 1)[1].split("\n}\ntrap cleanup", 1)[0]
        self.assertIn("emit_redacted_compose_logs node-agent 150", cleanup)
        self.assertIn("emit_redacted_compose_logs mediamtx 150", cleanup)
        self.assertIn("emit_redacted_compose_logs control-ui 100", cleanup)
        self.assertNotIn('logs --no-color --tail=150 node-agent >&2', cleanup)
        self.assertNotIn('logs --no-color --tail=150 mediamtx >&2', cleanup)
        self.assertNotIn('logs --no-color --tail=100 control-ui >&2', cleanup)

        helper = self.source.split("emit_redacted_compose_logs() {", 1)[1].split(
            "\n}\n\ncleanup() {", 1
        )[0]
        self.assertIn('>"$raw_file" 2>&1 || logs_rc=$?', helper)
        self.assertIn('redact_ingest_credentials <"$raw_file" >&2', helper)
        self.assertIn("output withheld", helper)
        self.assertNotIn('cat "$raw_file"', helper)

    def test_redactor_does_not_put_ingest_secret_on_python_argv(self) -> None:
        helper = self.source.split("redact_ingest_credentials() {", 1)[1].split(
            "\n}\n\nemit_redacted_compose_logs() {", 1
        )[0]
        self.assertIn('"$redaction_values"', helper)
        self.assertNotIn('"$ingest_secret"', helper)
        self.assertNotIn('"$ingest_username"', helper)
        self.assertIn('Path(sys.argv[1]).read_bytes()', helper)

    def test_diagnostics_are_withheld_if_credential_was_issued_before_redaction(self) -> None:
        credential_block = self.source.split('credential="$(curl', 1)[1].split(
            'export ASSIGNED_PROVIDER_SERVER_ID', 1
        )[0]
        self.assertLess(
            credential_block.index("credential_material_obtained=1"),
            credential_block.index("ingest_username="),
        )
        self.assertIn("write_redaction_values", credential_block)
        self.assertIn("redaction_has_ingest_secret=1", credential_block)

        helper = self.source.split("emit_redacted_compose_logs() {", 1)[1].split(
            "\n}\n\ncleanup() {", 1
        )[0]
        self.assertIn("credential_material_obtained == 1", helper)
        self.assertIn("redaction_has_ingest_secret != 1", helper)
        self.assertIn("diagnostics withheld", helper)

    def test_script_does_not_preemptively_down_an_existing_stack(self) -> None:
        before_first_up = self.source.split('"${compose[@]}" up -d --build control-ui', 1)[0]
        self.assertNotIn('"${compose[@]}" down', before_first_up.split("trap cleanup EXIT", 1)[1])
        self.assertIn('"${compose[@]}" config >/dev/null', before_first_up)


if __name__ == "__main__":
    unittest.main()
