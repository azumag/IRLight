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

    def _run_harness(self, *, failing_smoke: str | None = None) -> tuple[subprocess.CompletedProcess[str], str]:
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
                "printf 'FAKE_DOCKER %s\\n' \"$*\"\n",
                encoding="utf-8",
            )
            docker.chmod(0o755)

            for smoke in self.smokes:
                path = root / smoke
                path.parent.mkdir(parents=True, exist_ok=True)
                exit_code = 7 if smoke == failing_smoke else 0
                path.write_text(
                    "#!/usr/bin/env bash\n"
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
            self.assertIn(f"smoke={smoke} result=PASS exit=0", completed.stdout)
            self.assertIn(f"| `{smoke}` | PASS | 0 |", summary)
        self.assertNotIn("Docker smoke runner diagnostics", completed.stderr)

    def test_failure_records_exit_code_and_secret_safe_runner_context(self) -> None:
        failing_smoke = self.smokes[0]
        completed, summary = self._run_harness(failing_smoke=failing_smoke)

        self.assertEqual(completed.returncode, 1)
        self.assertIn(
            f"smoke={failing_smoke} result=FAIL exit=7",
            completed.stdout,
        )
        self.assertIn("Docker smoke failures (1):", completed.stderr)
        self.assertIn(f"{failing_smoke}:7", completed.stderr)
        self.assertIn("Docker smoke runner diagnostics (secret-safe)", completed.stderr)
        self.assertIn("FAKE_DOCKER system df", completed.stderr)
        self.assertIn("FAKE_DOCKER compose ls --all", completed.stderr)
        self.assertIn(
            "FAKE_DOCKER ps -a --format table {{.Names}}\\t{{.Image}}\\t{{.Status}}",
            completed.stderr,
        )
        self.assertIn(f"| `{failing_smoke}` | FAIL | 7 |", summary)

    def test_failure_context_does_not_use_secret_prone_docker_inspect(self) -> None:
        self.assertNotIn("docker inspect", self.source)


if __name__ == "__main__":
    unittest.main()
