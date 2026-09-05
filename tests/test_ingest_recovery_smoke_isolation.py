from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = {
    "srt": ROOT / "scripts" / "smoke-srt-ingest-recovery.sh",
    "rtmps": ROOT / "scripts" / "smoke-rtmps-ingest-recovery.sh",
}


class IngestRecoverySmokeIsolationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sources = {
            name: path.read_text(encoding="utf-8") for name, path in SCRIPTS.items()
        }

    def test_compose_projects_are_unique_per_run(self) -> None:
        expected_projects = {
            "srt": 'smoke_project="irlight-srt-ingest-recovery-smoke-$$-$RANDOM"',
            "rtmps": 'smoke_project="irlight-rtmps-ingest-recovery-smoke-$$-$RANDOM"',
        }
        expected_compose = (
            'compose=(docker compose -p "$smoke_project" '
            '-f "$repo_root/docker-compose.poc.yml" -f "$override")'
        )
        for name, source in self.sources.items():
            with self.subTest(script=name):
                self.assertIn(expected_projects[name], source)
                self.assertIn(expected_compose, source)
                self.assertNotIn("COMPOSE_PROJECT_NAME", source)

    def test_cleanup_only_targets_generated_project(self) -> None:
        for name, source in self.sources.items():
            with self.subTest(script=name):
                cleanup = source.split("cleanup() {", 1)[1].split(
                    "\n}\ntrap cleanup", 1
                )[0]
                self.assertIn(
                    '"${compose[@]}" down --volumes --remove-orphans', cleanup
                )
                self.assertNotIn("docker compose down", cleanup)
                self.assertNotIn("down -v", cleanup)

    def test_scripts_do_not_preemptively_stop_existing_stack(self) -> None:
        for name, source in self.sources.items():
            with self.subTest(script=name):
                before_up = source.split('"${compose[@]}" up -d --build control-ui', 1)[0]
                after_trap = before_up.split("trap cleanup EXIT", 1)[1]
                self.assertNotIn('"${compose[@]}" down', after_trap)
                self.assertIn('"${compose[@]}" config >/dev/null', after_trap)

    def test_temporary_material_is_private_and_run_scoped(self) -> None:
        for name, source in self.sources.items():
            with self.subTest(script=name):
                self.assertIn("umask 077", source)
                self.assertIn('tmp_dir="$(mktemp -d)"', source)
                self.assertIn('cookie_jar="$tmp_dir/cookies.txt"', source)
                self.assertIn('publisher_log="$tmp_dir/', source)
        rtmps = self.sources["rtmps"]
        self.assertIn('-keyout "$tmp_dir/server.key"', rtmps)
        self.assertIn('-out "$tmp_dir/server.crt"', rtmps)

    def test_compose_files_are_resolved_from_repository_root(self) -> None:
        expected_root = (
            'repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"'
        )
        for name, source in self.sources.items():
            with self.subTest(script=name):
                self.assertIn(expected_root, source)
                self.assertIn('-f "$repo_root/docker-compose.poc.yml"', source)


if __name__ == "__main__":
    unittest.main()
