from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "smoke-rtmps.sh"


class RtmpsSmokeIsolationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SCRIPT.read_text(encoding="utf-8")

    def test_compose_project_is_unique_per_run(self) -> None:
        self.assertIn('smoke_project="irlight-rtmps-smoke-$$-$RANDOM"', self.source)
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
        before_up = self.source.split('"${compose[@]}" up -d --build', 1)[0]
        self.assertNotIn('"${compose[@]}" down', before_up.split("trap cleanup EXIT", 1)[1])
        self.assertIn('"${compose[@]}" config >/dev/null', before_up)

    def test_runtime_secrets_are_private_temp_artifacts(self) -> None:
        self.assertIn("umask 077", self.source)
        self.assertIn('cookie_jar="$tmp_dir/cookies.txt"', self.source)
        self.assertIn('-keyout "$tmp_dir/server.key"', self.source)
        self.assertIn('chmod 600 "$tmp_dir/server.key"', self.source)


if __name__ == "__main__":
    unittest.main()
