from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
CORE_SCRIPTS = {
    "srt": ROOT / "scripts" / "smoke-srt-ingest-recovery-core.sh",
    "rtmps": ROOT / "scripts" / "smoke-rtmps-ingest-recovery-core.sh",
}
WRAPPERS = {
    "srt": ROOT / "scripts" / "smoke-srt-ingest-recovery.sh",
    "rtmps": ROOT / "scripts" / "smoke-rtmps-ingest-recovery.sh",
}


class IngestRecoverySmokeIsolationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sources = {
            name: path.read_text(encoding="utf-8")
            for name, path in CORE_SCRIPTS.items()
        }
        cls.wrappers = {
            name: path.read_text(encoding="utf-8")
            for name, path in WRAPPERS.items()
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

    def test_rtmps_listener_probe_drains_openssl_before_marker_search(self) -> None:
        rtmps = self.sources["rtmps"]
        wait = rtmps.split("wait_rtmps_listener() {", 1)[1].split(
            "\n}\n\nlogin()", 1
        )[0]
        self.assertIn(
            'local probe_output="$tmp_dir/rtmps-listener-probe.txt"',
            wait,
        )
        self.assertIn(
            'if timeout 4 openssl s_client -connect 127.0.0.1:1936 -servername localhost \\\n'
            '      </dev/null >"$probe_output" 2>/dev/null; then',
            wait,
        )
        self.assertIn(
            "if grep -Fq 'BEGIN CERTIFICATE' \"$probe_output\"; then",
            wait,
        )
        self.assertNotIn("| grep", wait)

    def test_rtmps_listener_probe_requires_successful_openssl_attempt(self) -> None:
        rtmps = self.sources["rtmps"]
        wait = rtmps.split("wait_rtmps_listener() {", 1)[1].split(
            "\n}\n\nlogin()", 1
        )[0]
        openssl_gate = wait.split(
            'if timeout 4 openssl s_client -connect 127.0.0.1:1936 -servername localhost',
            1,
        )[1].split("\n    fi", 1)[0]
        self.assertIn(
            "if grep -Fq 'BEGIN CERTIFICATE' \"$probe_output\"; then",
            openssl_gate,
        )
        self.assertIn("return 0", openssl_gate)
        self.assertNotIn("|| true", openssl_gate)

    def test_public_wrappers_quarantine_all_inner_output(self) -> None:
        expected_cores = {
            "srt": "smoke-srt-ingest-recovery-core.sh",
            "rtmps": "smoke-rtmps-ingest-recovery-core.sh",
        }
        expected_stages = {
            "srt": "stage=srt-ingest-recovery-quarantined",
            "rtmps": "stage=rtmps-ingest-recovery-quarantined",
        }
        for name, source in self.wrappers.items():
            with self.subTest(script=name):
                self.assertIn("umask 077", source)
                self.assertIn('tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/irlight-', source)
                self.assertIn(
                    f'bash "$script_dir/{expected_cores[name]}" >"$raw_log" 2>&1 &',
                    source,
                )
                self.assertIn(expected_stages[name], source)
                self.assertIn(
                    "inner diagnostics withheld because this smoke carries generated ingest credentials",
                    source,
                )
                self.assertNotIn('cat "$raw_log"', source)
                self.assertNotIn('tail ', source)
                self.assertNotIn('sed ', source)
                self.assertNotIn('awk ', source)
                self.assertNotIn('grep ', source)

    def test_public_wrappers_remove_private_logs_and_forward_signals(self) -> None:
        for name, source in self.wrappers.items():
            with self.subTest(script=name):
                self.assertIn('rm -rf "$tmp_dir"', source)
                self.assertIn("trap cleanup_wrapper EXIT", source)
                self.assertIn("trap 'forward_signal 130' INT", source)
                self.assertIn("trap 'forward_signal 143' TERM", source)
                self.assertIn('kill -TERM "$core_pid"', source)
                self.assertIn('wait "$core_pid"', source)

    def test_generated_credentials_remain_inside_quarantined_core(self) -> None:
        self.assertIn(
            'streamid="publish:live/input:${ingest_username}:${ingest_secret}"',
            self.sources["srt"],
        )
        self.assertIn(
            'rtmps_url="${rtmps_server_url}?user=${ingest_username}&pass=${ingest_secret}"',
            self.sources["rtmps"],
        )
        for name, wrapper in self.wrappers.items():
            with self.subTest(script=name):
                self.assertNotIn("ingest_secret", wrapper)
                self.assertNotIn("credential_secret", wrapper)


if __name__ == "__main__":
    unittest.main()
