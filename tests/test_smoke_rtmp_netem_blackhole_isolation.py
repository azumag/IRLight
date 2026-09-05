from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "smoke-rtmp-netem-blackhole.sh"


class RtmpNetemBlackholeSmokeIsolationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SCRIPT.read_text(encoding="utf-8")

    def test_compose_project_is_unique_per_run(self) -> None:
        self.assertIn(
            'smoke_project="irlight-rtmp-netem-blackhole-smoke-$$-$RANDOM"',
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
        self.assertIn('override="$tmp_dir/rtmp-netem.override.yml"', self.source)
        self.assertIn('publisher_log="$tmp_dir/publisher.log"', self.source)
        self.assertNotIn("/tmp/irlight-rtmp-netem-publisher.log", self.source)

    def test_netem_helper_is_resolved_from_repository_root(self) -> None:
        self.assertIn(
            'bash "$repo_root/scripts/netem-container.sh" apply "$publisher_name" --loss 100',
            self.source,
        )
        self.assertIn(
            'bash "$repo_root/scripts/netem-container.sh" clear "$publisher_name"',
            self.source,
        )
        self.assertNotIn("bash ./scripts/netem-container.sh", self.source)


if __name__ == "__main__":
    unittest.main()
