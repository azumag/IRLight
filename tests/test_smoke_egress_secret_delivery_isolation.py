from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "smoke-egress-secret-delivery.sh"


class EgressSecretDeliverySmokeIsolationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SCRIPT.read_text(encoding="utf-8")

    def test_compose_project_is_unique_per_run(self) -> None:
        self.assertIn(
            'smoke_project="irlight-egress-secret-delivery-smoke-$$-$RANDOM"',
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
            '"${compose[@]}" up -d --build control-ui', 1
        )[0]
        after_trap = before_up.split("trap cleanup EXIT", 1)[1]
        self.assertNotIn('"${compose[@]}" down', after_trap)
        self.assertIn('"${compose[@]}" config >/dev/null', after_trap)

    def test_temporary_auth_material_is_private(self) -> None:
        self.assertIn("umask 077", self.source)
        self.assertIn('tmp_dir="$(mktemp -d)"', self.source)
        self.assertIn('cookie_jar="$tmp_dir/cookies.txt"', self.source)
        self.assertIn('override="$tmp_dir/egress-secret.override.yml"', self.source)

    def test_failure_cleanup_quarantines_service_logs(self) -> None:
        cleanup = self.source.split("cleanup() {", 1)[1].split("\n}\ntrap cleanup", 1)[0]
        self.assertIn('withhold_sensitive_diagnostics "control-ui/service"', cleanup)
        self.assertIn('withhold_sensitive_diagnostics "node-agent/service"', cleanup)
        self.assertNotIn("logs --no-color", cleanup)
        self.assertNotIn('"${compose[@]}" logs', cleanup)

    def test_quarantine_message_contains_no_runtime_secret_values(self) -> None:
        helper = self.source.split("withhold_sensitive_diagnostics() {", 1)[1].split(
            "\n}\n\ncleanup() {", 1
        )[0]
        self.assertNotIn("$stream_key", helper)
        self.assertNotIn("$cookie_jar", helper)
        self.assertNotIn("$bootstrap_token", helper)
        self.assertNotIn("$csrf", helper)
        self.assertNotIn("$expected_url", helper)

    def test_secret_bearing_api_assertions_do_not_echo_payloads(self) -> None:
        self.assertNotIn('assert d.get("configured") is True, d', self.source)
        self.assertNotIn(
            'assert d.get("destination_id") == sys.argv[1], d',
            self.source,
        )
        self.assertIn(
            '"Destination secret API did not confirm configuration"',
            self.source,
        )
        self.assertIn(
            '"Session prepare response destination_id mismatch"',
            self.source,
        )

    def test_prepare_secret_leak_check_precedes_response_parsing(self) -> None:
        prepared_start = self.source.index('prepared="$(curl')
        prepared_section = self.source[prepared_start : self.source.index(
            'export ASSIGNED_PROVIDER_SERVER_ID', prepared_start
        )]
        leak_check = prepared_section.index('grep -Fq "$stream_key" <<<"$prepared"')
        provider_parse = prepared_section.index('provider_server_id="$(python3')
        self.assertLess(leak_check, provider_parse)

    def test_non_leak_and_file_mode_contract_remains_intact(self) -> None:
        self.assertIn('grep -Fq "$stream_key" <<<"$secret_response"', self.source)
        self.assertIn('grep -Fq "$stream_key" <<<"$secret_state"', self.source)
        self.assertIn('destination_secrets.json expected mode 600', self.source)
        self.assertIn('Node egress_url expected mode 600', self.source)
        self.assertIn('grep -Fq "$stream_key" <<<"$state_dump"', self.source)
        self.assertIn('grep -Fq "$expected_url" <<<"$state_dump"', self.source)
        self.assertIn('grep -Fq "$stream_key" <<<"$node_listing"', self.source)
        self.assertIn('grep -Fq "$expected_url" <<<"$node_listing"', self.source)


if __name__ == "__main__":
    unittest.main()
