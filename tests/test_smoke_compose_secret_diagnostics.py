from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "scripts" / "smoke-compose.sh"
CORE = ROOT / "scripts" / "smoke-compose-core.sh"


class SmokeComposeSecretDiagnosticsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.wrapper = WRAPPER.read_text(encoding="utf-8")
        cls.core = CORE.read_text(encoding="utf-8")

    def test_public_entrypoint_quarantines_inner_output_in_private_file(self) -> None:
        self.assertIn("umask 077", self.wrapper)
        self.assertIn(
            'tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/irlight-smoke-compose-wrapper.XXXXXX")"',
            self.wrapper,
        )
        self.assertIn('raw_log="$tmp_dir/smoke-compose.raw.log"', self.wrapper)
        self.assertIn(
            'bash "$script_dir/smoke-compose-core.sh" >"$raw_log" 2>&1 &',
            self.wrapper,
        )
        self.assertIn('rm -rf "$tmp_dir"', self.wrapper)

    def test_failure_path_never_replays_quarantined_diagnostics(self) -> None:
        self.assertNotIn('cat "$raw_log"', self.wrapper)
        self.assertNotIn('tail -n 20 "$raw_log"', self.wrapper)
        self.assertNotIn('sed ', self.wrapper)
        self.assertIn("inner diagnostics withheld", self.wrapper)

    def test_known_port_collision_is_normalized_without_echoing_matching_line(self) -> None:
        self.assertIn("grep -Eiq", self.wrapper)
        self.assertIn('"$raw_log"', self.wrapper)
        self.assertIn('echo "host port is already allocated" >&2', self.wrapper)
        self.assertNotIn('grep -Ei ', self.wrapper)
        self.assertNotIn('echo "docker compose', self.wrapper)

    def test_wrapper_forwards_termination_to_core_cleanup(self) -> None:
        self.assertIn("trap 'forward_signal 130' INT", self.wrapper)
        self.assertIn("trap 'forward_signal 143' TERM", self.wrapper)
        self.assertIn('kill -TERM "$core_pid"', self.wrapper)
        self.assertIn('wait "$core_pid"', self.wrapper)

    def test_original_smoke_logic_remains_in_private_core(self) -> None:
        self.assertIn('ingest_secret=""', self.core)
        self.assertIn('smoke_project="irlight-poc-smoke-$$-$RANDOM"', self.core)
        self.assertNotIn("smoke-compose-core.sh", self.core)


if __name__ == "__main__":
    unittest.main()
