from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "scripts" / "ci-docker-smoke-suite.sh"


class DockerSmokeSuiteDiagnosticsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SUITE.read_text(encoding="utf-8")
        cls.smokes = cls._listed_smokes(cls.source)
        if not cls.smokes:
            raise AssertionError("Docker smoke suite did not list any scenarios")

    @staticmethod
    def _listed_smokes(source: str) -> list[str]:
        smokes: list[str] = []
        in_array = False
        for raw_line in source.splitlines():
            line = raw_line.strip()
            if line == "smokes=(":
                in_array = True
                continue
            if in_array and line == ")":
                break
            if in_array and line.startswith("scripts/"):
                smokes.append(line)
        return smokes

    def _run_harness(
        self,
        *,
        failing_smoke: str | None = None,
        failure_stage: str = "compose-control-up",
    ) -> tuple[subprocess.CompletedProcess[str], str]:
        with tempfile.TemporaryDirectory(prefix="irlight-docker-smoke-diagnostics-") as tmp:
            root = Path(tmp)
            fake_bin = root / "bin"
            fake_bin.mkdir()

            timeout = fake_bin / "timeout"
            timeout.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "shift 3\n"
                "exec \"$@\"\n",
                encoding="utf-8",
            )
            timeout.chmod(0o755)

            docker = fake_bin / "docker"
            docker.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "printf 'FAKE_DOCKER %s\\n' \"$*\" >&2\n"
                "if [[ \"${1:-}\" == \"ps\" ]]; then\n"
                "  printf '%s\\n' "
                "'irlight-ci|control|irlight-ci-control-1|irlight/control:ci|exited|Exited (7) 1 second ago'\n"
                "fi\n"
                "if [[ -f failed-scenario-state ]]; then\n"
                "  printf 'FAKE_DOCKER_STATE failed-scenario-present\\n' >&2\n"
                "fi\n",
                encoding="utf-8",
            )
            docker.chmod(0o755)

            for smoke in self.smokes:
                path = root / smoke
                path.parent.mkdir(parents=True, exist_ok=True)
                exit_code = 7 if smoke == failing_smoke else 0
                diagnostic = ""
                marker = "rm -f failed-scenario-state\n"
                if exit_code:
                    marker = "touch failed-scenario-state\n"
                    diagnostic = (
                        "printf '%s\\n' "
                        f"'::error title=IRLight docker smoke failure::stage={failure_stage}%0A"
                        "credential=AUDIT_DUMMY_SECRET' >&2\n"
                    )
                path.write_text(
                    "#!/usr/bin/env bash\n"
                    f"{marker}"
                    f"{diagnostic}"
                    f"exit {exit_code}\n",
                    encoding="utf-8",
                )
                path.chmod(0o755)

            summary = root / "step-summary.md"
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
            env["GITHUB_STEP_SUMMARY"] = str(summary)
            completed = subprocess.run(
                ["bash", str(SUITE)],
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            return completed, summary.read_text(encoding="utf-8")

    def test_success_records_each_scenario_in_log_and_step_summary(self) -> None:
        completed, summary = self._run_harness()

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            completed.stdout.count("IRLIGHT_DOCKER_SMOKE_RESULT"),
            len(self.smokes),
        )
        for smoke in self.smokes:
            self.assertIn(
                f"smoke={smoke} result=PASS exit=0",
                completed.stdout,
            )
            self.assertIn("stage=-", completed.stdout)
            self.assertIn(f"| `{smoke}` | PASS | 0 |", summary)
        self.assertNotIn("Docker smoke runner diagnostics", completed.stderr)
        self.assertNotIn("Failure context:", summary)
        self.assertNotIn("Runner resources before scenario", summary)

    def test_failure_records_exit_code_stage_and_secret_safe_runner_context(self) -> None:
        failing_smoke = self.smokes[0]
        completed, summary = self._run_harness(failing_smoke=failing_smoke)

        self.assertEqual(completed.returncode, 1)
        self.assertIn(
            f"smoke={failing_smoke} result=FAIL exit=7",
            completed.stdout,
        )
        self.assertIn("stage=compose-control-up", completed.stdout)
        self.assertIn("Docker smoke failures (1):", completed.stderr)
        self.assertIn(
            f"{failing_smoke}:7:compose-control-up",
            completed.stderr,
        )
        self.assertIn(
            f"Docker smoke runner diagnostics (secret-safe; scenario={failing_smoke})",
            completed.stderr,
        )
        self.assertIn("Runner resources before scenario:", completed.stderr)
        self.assertIn("Runner resources at failure boundary:", completed.stderr)
        self.assertGreaterEqual(completed.stderr.count("FAKE_DOCKER system df"), 2)
        self.assertIn("FAKE_DOCKER compose ls --all", completed.stderr)
        self.assertIn(
            "FAKE_DOCKER ps -a --filter label=com.docker.compose.project",
            completed.stderr,
        )
        self.assertIn(
            "Compose container state (project/service/name/image/state/status only):",
            completed.stderr,
        )
        self.assertIn(
            "irlight-ci|control|irlight-ci-control-1|irlight/control:ci|exited|Exited (7) 1 second ago",
            completed.stderr,
        )
        self.assertIn(f"| `{failing_smoke}` | FAIL | 7 |", summary)
        self.assertIn("| `compose-control-up` |", summary)
        self.assertIn(f"#### Failure context: `{failing_smoke}`", summary)
        self.assertIn("<summary>Runner resources before scenario</summary>", summary)
        self.assertIn("<summary>Runner resources at failure boundary</summary>", summary)
        self.assertEqual(summary.count("FAKE_DOCKER system df"), 2)
        self.assertIn(
            "| `irlight-ci` | `control` | `irlight-ci-control-1` | `irlight/control:ci` | `exited` | Exited (7) 1 second ago |",
            summary,
        )
        self.assertNotIn("AUDIT_DUMMY_SECRET", summary)

    def test_failure_context_is_captured_before_later_smoke_cleanup(self) -> None:
        failing_smoke = self.smokes[0]
        completed, _ = self._run_harness(failing_smoke=failing_smoke)

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(
            completed.stderr.count("Docker smoke runner diagnostics (secret-safe; scenario="),
            1,
        )
        self.assertIn("FAKE_DOCKER_STATE failed-scenario-present", completed.stderr)

    def test_failure_resource_baseline_is_retained_for_before_after_comparison(self) -> None:
        failing_smoke = self.smokes[0]
        completed, summary = self._run_harness(failing_smoke=failing_smoke)

        self.assertEqual(completed.returncode, 1)
        before = summary.index("<summary>Runner resources before scenario</summary>")
        after = summary.index("<summary>Runner resources at failure boundary</summary>")
        self.assertLess(before, after)
        self.assertEqual(summary.count("Docker storage:"), 2)
        self.assertEqual(summary.count("Filesystem:"), 2)

    def test_failure_stage_is_allowlisted_before_entering_compact_outputs(self) -> None:
        failing_smoke = self.smokes[0]
        completed, summary = self._run_harness(
            failing_smoke=failing_smoke,
            failure_stage="node-auth-ready",
        )

        result_lines = [
            line
            for line in completed.stdout.splitlines()
            if line.startswith("IRLIGHT_DOCKER_SMOKE_RESULT")
            and f"smoke={failing_smoke}" in line
        ]
        self.assertEqual(len(result_lines), 1)
        self.assertIn("stage=node-auth-ready", result_lines[0])
        self.assertNotIn("AUDIT_DUMMY_SECRET", result_lines[0])
        self.assertIn("| `node-auth-ready` |", summary)
        self.assertNotIn("AUDIT_DUMMY_SECRET", summary)

    def test_resource_probes_are_timeout_bounded(self) -> None:
        self.assertIn(
            "timeout --signal=TERM --kill-after=2s 10s df -h /",
            self.source,
        )
        self.assertIn(
            "timeout --signal=TERM --kill-after=2s 10s docker system df",
            self.source,
        )

    def test_failure_context_does_not_use_secret_prone_docker_inspect(self) -> None:
        self.assertNotIn("docker inspect", self.source)
        self.assertIn("--filter label=com.docker.compose.project", self.source)
        self.assertIn('com.docker.compose.service', self.source)
        self.assertIn("docker system df", self.source)


if __name__ == "__main__":
    unittest.main()
