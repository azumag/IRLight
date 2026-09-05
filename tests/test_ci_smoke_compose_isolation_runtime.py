from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parent.parent
WRAPPER = ROOT / "scripts" / "ci-smoke-compose-isolation-runtime.sh"
SUITE = ROOT / "scripts" / "ci-docker-smoke-suite.sh"


class ComposeSmokeRuntimeIsolationContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.wrapper = WRAPPER.read_text(encoding="utf-8")
        cls.suite = SUITE.read_text(encoding="utf-8")

    def test_shared_suite_runs_central_smoke_through_runtime_wrapper(self) -> None:
        self.assertIn(
            "scripts/ci-smoke-compose-isolation-runtime.sh",
            self.suite,
        )
        self.assertNotIn("\n  scripts/smoke-compose.sh\n", self.suite)

    def test_wrapper_uses_private_run_scoped_resources(self) -> None:
        for expected in (
            "umask 077",
            'mktemp -d "${TMPDIR:-/tmp}/irlight-smoke-isolation-runtime.XXXXXX"',
            'sentinel_project="irlight-smoke-sentinel-$$-$RANDOM"',
            'docker compose -p "$sentinel_project"',
            '"${sentinel_compose[@]}" down -v --remove-orphans',
        ):
            self.assertIn(expected, self.wrapper)

    def test_wrapper_exercises_real_overlap_without_reusing_a_project(self) -> None:
        for expected in (
            'bash "$repo_root/scripts/smoke-compose.sh" >"$primary_log" 2>&1 &',
            'timeout --signal=TERM 120 bash "$repo_root/scripts/smoke-compose.sh"',
            "overlapping smoke unexpectedly succeeded despite fixed host ports",
            "overlapping smoke did not fail promptly on the host-port collision",
            "primary smoke became unhealthy after overlapping smoke cleanup",
        ):
            self.assertIn(expected, self.wrapper)

    def test_wrapper_proves_unrelated_container_and_volume_survive(self) -> None:
        for expected in (
            "sentinel_container_id=",
            "sentinel_volume_name=",
            "current_sentinel_container_id=",
            "unrelated sentinel container identity changed during the smoke",
            "unrelated sentinel volume identity changed during the smoke",
            "unrelated sentinel container was stopped by the smoke",
            "unrelated sentinel volume contents changed during the smoke",
        ):
            self.assertIn(expected, self.wrapper)


if __name__ == "__main__":
    unittest.main()
