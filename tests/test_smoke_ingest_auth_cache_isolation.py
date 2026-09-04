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
        self.assertIn('>"$publisher_log" 2>&1 &', self.source)
        self.assertNotIn("/tmp/irlight-cache-smoke-cookies.txt", self.source)
        self.assertNotIn("/tmp/irlight-cache-publisher.log", self.source)

    def test_script_does_not_preemptively_down_an_existing_stack(self) -> None:
        before_first_up = self.source.split('"${compose[@]}" up -d --build control-ui', 1)[0]
        self.assertNotIn('"${compose[@]}" down', before_first_up.split("trap cleanup EXIT", 1)[1])
        self.assertIn('"${compose[@]}" config >/dev/null', before_first_up)


if __name__ == "__main__":
    unittest.main()
