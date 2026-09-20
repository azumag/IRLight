from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "smoke-egress-dns-tls.sh"


class EgressDnsTlsSmokeIsolationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SCRIPT.read_text(encoding="utf-8")

    def test_compose_project_is_unique_per_run(self) -> None:
        self.assertIn(
            'smoke_project="irlight-egress-dns-tls-smoke-$$-$RANDOM"',
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
            '"${compose[@]}" up -d --build mediamtx', 1
        )[0]
        self.assertNotIn('"${compose[@]}" down', before_up.split("trap cleanup EXIT", 1)[1])
        self.assertIn('"${compose[@]}" config >/dev/null', before_up)

    def test_tls_material_and_destination_secrets_are_private(self) -> None:
        self.assertIn("umask 077", self.source)
        self.assertIn('dns_secret="$tmp_dir/dns-egress-url"', self.source)
        self.assertIn('tls_secret="$tmp_dir/tls-egress-url"', self.source)
        self.assertIn('chmod 600 "$dns_secret"', self.source)
        self.assertIn('chmod 600 "$tls_secret"', self.source)
        self.assertIn('chmod 600 "$tmp_dir/server.key"', self.source)

    def test_failure_diagnostics_redact_generated_stream_key(self) -> None:
        cleanup = self.source.split("cleanup() {", 1)[1].split("\n}\ntrap cleanup", 1)[0]
        self.assertIn("redact_generated_secrets() {", self.source)
        self.assertIn("emit_redacted_compose_logs() {", self.source)
        self.assertIn("emit_redacted_compose_logs egress-dns 120", cleanup)
        self.assertIn("emit_redacted_compose_logs egress-tls 160", cleanup)
        self.assertIn("emit_redacted_compose_logs egress-tls-target 120", cleanup)
        self.assertNotIn('"${compose[@]}" logs --no-color --tail=120 egress-dns >&2', cleanup)
        self.assertNotIn('"${compose[@]}" logs --no-color --tail=160 egress-tls >&2', cleanup)
        self.assertIn("output withheld", self.source)

    def test_status_timeout_diagnostics_are_redacted(self) -> None:
        wait_body = self.source.split("wait_status_reason() {", 1)[1].split(
            "\n}\n\n# The generated project", 1
        )[0]
        self.assertIn("redact_generated_secrets", wait_body)
        self.assertIn("last=<redaction-failed>", wait_body)
        self.assertNotIn("last=$payload", wait_body)

    def test_secret_log_check_captures_before_searching(self) -> None:
        self.assertIn('egress_tls_logs="$tmp_dir/egress-tls.log"', self.source)
        self.assertIn(
            '"${compose[@]}" logs --no-color egress-tls >"$egress_tls_logs" 2>&1',
            self.source,
        )
        self.assertIn('grep -Fq "$stream_key" "$egress_tls_logs"', self.source)
        self.assertNotIn(
            '"${compose[@]}" logs --no-color egress-tls | grep -Fq "$stream_key"',
            self.source,
        )
        self.assertIn(
            "failed to read egress TLS logs for secret redaction check",
            self.source,
        )


if __name__ == "__main__":
    unittest.main()
