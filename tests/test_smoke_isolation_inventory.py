from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

# Temporary exception tracked by issue #85. Keep this list intentionally small:
# adding a new Compose smoke must use the isolated pattern from the start.
KNOWN_LEGACY_COMPOSE_SMOKES = {"smoke-compose.sh"}


class SmokeIsolationInventoryTest(unittest.TestCase):
    def compose_smokes(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for script in sorted(SCRIPTS.glob("smoke-*.sh")):
            source = script.read_text(encoding="utf-8")
            if "docker compose" in source:
                result[script.name] = source
        return result

    def test_all_compose_smokes_except_tracked_legacy_harness_are_isolated(self) -> None:
        compose_smokes = self.compose_smokes()
        self.assertIn("smoke-compose.sh", compose_smokes)

        generated_project = re.compile(
            r'^smoke_project="[^"\n]*\$\$-\$RANDOM"$',
            re.MULTILINE,
        )

        for name, source in compose_smokes.items():
            if name in KNOWN_LEGACY_COMPOSE_SMOKES:
                continue

            with self.subTest(script=name):
                self.assertIn("docker compose -p ", source)
                self.assertNotIn("IRLIGHT_SMOKE_PROJECT", source)
                self.assertRegex(
                    source,
                    generated_project,
                    msg=f"{name} must generate a run-scoped Compose project",
                )

    def test_legacy_exception_is_specific_and_still_requires_migration(self) -> None:
        self.assertEqual(KNOWN_LEGACY_COMPOSE_SMOKES, {"smoke-compose.sh"})
        source = (SCRIPTS / "smoke-compose.sh").read_text(encoding="utf-8")

        # These assertions make the exception self-expiring: once #85 removes
        # the unsafe compatibility behavior, this test must be updated rather
        # than silently leaving a permanent allow-list entry behind.
        self.assertIn("IRLIGHT_SMOKE_PROJECT", source)
        self.assertIn('"${compose[@]}" down --remove-orphans', source)
        self.assertIn("/tmp/irlight-smoke-cookies.txt", source)


if __name__ == "__main__":
    unittest.main()
