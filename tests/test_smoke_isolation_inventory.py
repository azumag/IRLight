from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


class SmokeIsolationInventoryTest(unittest.TestCase):
    def compose_smokes(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for script in sorted(SCRIPTS.glob("smoke-*.sh")):
            source = script.read_text(encoding="utf-8")
            if "docker compose" in source:
                result[script.name] = source
        return result

    def test_all_compose_smokes_are_isolated(self) -> None:
        compose_smokes = self.compose_smokes()
        self.assertIn("smoke-compose.sh", compose_smokes)

        generated_project = re.compile(
            r'^smoke_project="[^"\n]*\$\$-\$RANDOM"$',
            re.MULTILINE,
        )

        for name, source in compose_smokes.items():
            with self.subTest(script=name):
                self.assertIn("docker compose -p ", source)
                self.assertNotIn("IRLIGHT_SMOKE_PROJECT", source)
                self.assertRegex(
                    source,
                    generated_project,
                    msg=f"{name} must generate a run-scoped Compose project",
                )

    def test_central_harness_uses_private_run_scoped_host_files(self) -> None:
        source = (SCRIPTS / "smoke-compose.sh").read_text(encoding="utf-8")

        self.assertIn("umask 077", source)
        self.assertIn('tmp_dir="$(mktemp -d ', source)
        self.assertIn('auth_cookie_jar="$tmp_dir/', source)
        self.assertIn('publisher_log="$tmp_dir/', source)
        self.assertIn('auth_reject_body="$tmp_dir/', source)
        self.assertNotIn("/tmp/irlight-", source)

    def test_central_harness_never_precleans_an_existing_project(self) -> None:
        source = (SCRIPTS / "smoke-compose.sh").read_text(encoding="utf-8")

        self.assertNotIn('"${compose[@]}" down --remove-orphans', source)
        self.assertIn('-f "$repo_root/docker-compose.poc.yml"', source)


if __name__ == "__main__":
    unittest.main()
