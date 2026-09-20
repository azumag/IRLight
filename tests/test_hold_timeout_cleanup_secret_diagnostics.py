from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SMOKE = ROOT / "scripts" / "smoke-hold-timeout-cleanup.sh"


class HoldTimeoutCleanupSecretDiagnosticsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SMOKE.read_text(encoding="utf-8")

    def test_failure_path_does_not_replay_control_logs(self) -> None:
        self.assertNotIn(
            '"${compose[@]}" logs --no-color --tail=180 control-ui >&2',
            self.source,
        )
        self.assertNotIn('echo "--- control logs ---" >&2', self.source)
        self.assertIn(
            "control-ui diagnostics withheld; HOLD_TIMEOUT smoke handles auth/session material",
            self.source,
        )

    def test_private_run_directory_and_nonsecret_state_evidence_are_preserved(self) -> None:
        self.assertIn("umask 077", self.source)
        self.assertIn('tmp_dir="$(mktemp -d)"', self.source)
        self.assertIn('echo "--- sessions state ---" >&2', self.source)
        self.assertIn('echo "--- fake provider state ---" >&2', self.source)

    def test_hold_timeout_contract_is_unchanged(self) -> None:
        self.assertIn('assert session.get("status") == "FINISHED"', self.source)
        self.assertIn('assert session.get("cleanup_pending") is False', self.source)
        self.assertIn('assert result.get("deadline_stops") == 0', self.source)
        self.assertIn('assert state.get("servers") == []', self.source)
        self.assertIn('assert state.get("volumes") == []', self.source)


if __name__ == "__main__":
    unittest.main()
