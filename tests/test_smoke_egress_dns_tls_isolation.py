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


if __name__ == "__main__":
    unittest.main()
