from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "soak-compose.sh"


class ComposeSoakIsolationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SCRIPT.read_text(encoding="utf-8")

    def test_compose_project_is_unique_per_run(self) -> None:
        self.assertIn('soak_project="irlight-poc-soak-$$-$RANDOM"', self.source)
        self.assertIn(
            'compose=(docker compose -p "$soak_project" -f "$repo_root/docker-compose.poc.yml")',
            self.source,
        )
        self.assertNotIn("IRLIGHT_SOAK_PROJECT", self.source)
        self.assertNotIn('COMPOSE_PROJECT_NAME:-', self.source)

    def test_cleanup_only_targets_generated_project(self) -> None:
        cleanup = self.source.split("cleanup() {", 1)[1].split("\n}\ntrap cleanup", 1)[0]
        self.assertIn(
            '"${compose[@]}" down --rmi local --volumes --remove-orphans',
            cleanup,
        )
        self.assertNotIn("docker compose down", cleanup)

    def test_script_does_not_preemptively_stop_existing_stack(self) -> None:
        before_up = self.source.split('"${compose[@]}" up -d --build', 1)[0]
        after_trap = before_up.split("trap cleanup EXIT", 1)[1]
        self.assertNotIn('"${compose[@]}" down', after_trap)
        self.assertIn('"${compose[@]}" config >/dev/null', after_trap)

    def test_compose_file_is_resolved_from_repository_root(self) -> None:
        self.assertIn(
            'repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"',
            self.source,
        )
        self.assertIn('-f "$repo_root/docker-compose.poc.yml"', self.source)


if __name__ == "__main__":
    unittest.main()
