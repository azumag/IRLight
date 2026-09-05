from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "smoke-egress-publish-conflict.sh"


class EgressPublishConflictSmokeIsolationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SCRIPT.read_text(encoding="utf-8")

    def test_compose_project_is_unique_per_run(self) -> None:
        self.assertIn(
            'smoke_project="irlight-egress-publish-conflict-smoke-$$-$RANDOM"',
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
        after_trap = before_up.split("trap cleanup EXIT", 1)[1]
        self.assertNotIn('"${compose[@]}" down', after_trap)
        self.assertIn('"${compose[@]}" config >/dev/null', after_trap)

    def test_temporary_secret_material_is_private(self) -> None:
        self.assertIn("umask 077", self.source)
        self.assertIn('tmp_dir="$(mktemp -d)"', self.source)
        self.assertIn('secret_file="$tmp_dir/egress_url"', self.source)
        self.assertIn('chmod 600 "$secret_file"', self.source)

    def test_conflict_target_is_ready_before_holder_starts(self) -> None:
        target_up = self.source.index(
            '"${compose[@]}" up -d egress-conflict-target'
        )
        listener_wait = self.source.index("wait_for_target_listener 30", target_up)
        holder_up = self.source.index(
            '"${compose[@]}" up -d --build mediamtx continuity control-ui node-agent conflict-holder'
        )
        self.assertLess(target_up, listener_wait)
        self.assertLess(listener_wait, holder_up)
        self.assertIn('grep -Fq "listener opened on :1935"', self.source)


if __name__ == "__main__":
    unittest.main()
