from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class EgressSmokeEarlyTempCleanupTest(unittest.TestCase):
    # Setup can fail before Compose resources necessarily exist, so the first
    # trap must stay temp-only until the normal cleanup handler replaces it.
    CASES = {
        "scripts/smoke-egress-reconnect.sh": 'printf \'%s\' "$stream_key" >"$stream_key_file"',
        "scripts/smoke-egress-dns-tls.sh": 'cat >"$dns_secret"',
        "scripts/smoke-egress-stop-terminal.sh": 'printf \'%s\\n\' "$stream_key" >"$redaction_values_file"',
        "scripts/smoke-egress-publish-conflict.sh": 'printf \'%s\\n\' "$stream_key" >"$redaction_values_file"',
    }

    def test_temp_only_exit_guard_is_armed_before_secret_material(self) -> None:
        for relative_path, first_secret_write in self.CASES.items():
            with self.subTest(script=relative_path):
                source = (ROOT / relative_path).read_text(encoding="utf-8")
                mktemp = source.index('tmp_dir="$(mktemp -d)"')
                early_trap = source.index('trap \'rm -rf "$tmp_dir"\' EXIT', mktemp)
                secret_write = source.index(first_secret_write, mktemp)
                full_trap = source.index("trap cleanup EXIT", early_trap)

                self.assertLess(mktemp, early_trap)
                self.assertLess(early_trap, secret_write)
                self.assertLess(secret_write, full_trap)

    def test_early_guard_is_temp_only_and_full_cleanup_replaces_it(self) -> None:
        expected_guard = 'trap \'rm -rf "$tmp_dir"\' EXIT'
        for relative_path in self.CASES:
            with self.subTest(script=relative_path):
                source = (ROOT / relative_path).read_text(encoding="utf-8")
                lines = source.splitlines()
                mktemp_line = lines.index('tmp_dir="$(mktemp -d)"')

                self.assertEqual(lines[mktemp_line + 1], expected_guard)
                self.assertEqual(source.count(expected_guard), 1)
                self.assertIn("cleanup() {", source)
                self.assertIn("trap cleanup EXIT", source)
                self.assertLess(source.index(expected_guard), source.index("trap cleanup EXIT"))


if __name__ == "__main__":
    unittest.main()
