from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "smoke-ingest-auth-abuse.sh"


class IngestAuthAbuseSmokeIsolationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SCRIPT.read_text(encoding="utf-8")

    def test_compose_project_is_unique_per_run(self) -> None:
        self.assertIn(
            'smoke_project="irlight-ingest-auth-abuse-smoke-$$-$RANDOM"',
            self.source,
        )
        self.assertIn(
            'compose=(docker compose -p "$smoke_project" '
            '-f docker-compose.poc.yml -f "$override")',
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
        self.assertNotIn('"${compose[@]}" down', before_up.split("trap cleanup EXIT", 1)[1])
        self.assertIn('"${compose[@]}" config >/dev/null', before_up)

    def test_sensitive_runtime_files_are_private_per_run(self) -> None:
        self.assertIn("umask 077", self.source)
        self.assertIn('cookie_jar="$tmp_dir/cookies.txt"', self.source)
        self.assertIn(
            'redaction_values="$tmp_dir/ingest-auth-abuse.redaction-values"',
            self.source,
        )
        self.assertNotIn("/tmp/irlight-auth-abuse-cookies.txt", self.source)

    def test_failure_diagnostics_are_redacted_without_raw_fallback(self) -> None:
        cleanup = self.source.split("cleanup() {", 1)[1].split("\n}\ntrap cleanup", 1)[0]
        self.assertIn("emit_redacted_compose_logs node-agent 150", cleanup)
        self.assertIn("emit_redacted_compose_logs control-ui 150", cleanup)
        self.assertNotIn('logs --no-color --tail=150 node-agent >&2', cleanup)
        self.assertNotIn('logs --no-color --tail=150 control-ui >&2', cleanup)

        helper = self.source.split("emit_redacted_compose_logs() {", 1)[1].split(
            "\n}\n\ncleanup() {", 1
        )[0]
        self.assertIn('>"$raw_file" 2>&1 || logs_rc=$?', helper)
        self.assertIn('redact_auth_values <"$raw_file" >&2', helper)
        self.assertIn("output withheld", helper)
        self.assertNotIn('cat "$raw_file"', helper)
        self.assertLess(
            helper.index("if (( logs_rc != 0 )); then"),
            helper.index('redact_auth_values <"$raw_file" >&2'),
        )

    def test_redactor_uses_private_value_file_not_secret_argv(self) -> None:
        helper = self.source.split("redact_auth_values() {", 1)[1].split(
            "\n}\n\nemit_redacted_compose_logs() {", 1
        )[0]
        self.assertIn('"$redaction_values"', helper)
        self.assertNotIn('"$wrong_secret"', helper)
        self.assertNotIn('"$csrf"', helper)
        self.assertNotIn('"$ingest_username"', helper)
        self.assertNotIn('"$ingest_secret"', helper)
        self.assertIn('Path(sys.argv[1]).read_bytes()', helper)

        writer = self.source.split("write_redaction_values() {", 1)[1].split(
            "\n}\n\nredact_auth_values() {", 1
        )[0]
        self.assertIn(
            '"$password" "$wrong_secret" "$csrf" "$ingest_username" "$ingest_secret"',
            writer,
        )
        self.assertIn('python3 - "$cookie_jar" >>"$redaction_values"', writer)
        self.assertIn('chmod 600 "$redaction_values"', writer)

    def test_login_material_is_secured_before_diagnostics_are_allowed(self) -> None:
        login = self.source.split("login() {", 1)[1].split("\n}\n\nauth_response() {", 1)[0]
        self.assertLess(
            login.index("session_material_obtained=1"),
            login.index("csrf="),
        )
        self.assertIn("write_redaction_values", login)
        self.assertIn("redaction_has_session_material=1", login)

        helper = self.source.split("emit_redacted_compose_logs() {", 1)[1].split(
            "\n}\n\ncleanup() {", 1
        )[0]
        self.assertIn("session_material_obtained == 1", helper)
        self.assertIn("redaction_has_session_material != 1", helper)
        self.assertIn("diagnostics withheld", helper)

    def test_diagnostics_are_withheld_until_issued_credential_is_secured(self) -> None:
        credential_block = self.source.split('credential="$(curl', 1)[1].split(
            "\n\nfor attempt in 1 2; do", 1
        )[0]
        self.assertLess(
            credential_block.index("credential_material_obtained=1"),
            credential_block.index("ingest_username="),
        )
        self.assertIn("ingest_secret=", credential_block)
        self.assertIn("write_redaction_values", credential_block)
        self.assertIn("redaction_has_ingest_credential=1", credential_block)

        helper = self.source.split("emit_redacted_compose_logs() {", 1)[1].split(
            "\n}\n\ncleanup() {", 1
        )[0]
        self.assertIn("credential_material_obtained == 1", helper)
        self.assertIn("redaction_has_ingest_credential != 1", helper)
        self.assertIn("diagnostics withheld", helper)

    def test_abuse_contract_remains_401_then_429_with_retry_after(self) -> None:
        self.assertIn('if [[ "$status" != "401" ]]', self.source)
        self.assertGreaterEqual(self.source.count('if [[ "$status" != "429" ]]'), 2)
        self.assertIn('if [[ -z "$retry_after" ]]', self.source)
        self.assertIn('grep -Fq "$wrong_secret" <<<"$state"', self.source)


if __name__ == "__main__":
    unittest.main()
