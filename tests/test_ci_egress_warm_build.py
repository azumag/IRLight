import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class EgressWarmBuildTests(unittest.TestCase):
    def setUp(self):
        self.workflow = (ROOT / ".github/workflows/ci.yml").read_text()
        self.assertIn("      - name: Warm egress image after cache pruning\n", self.workflow)
        self.step = self.workflow.split(
            "      - name: Warm egress image after cache pruning\n", 1
        )[1].split("      - name:", 1)[0]

    def test_warm_build_runs_after_prune_before_suite_and_is_bounded(self):
        self.assertLess(self.workflow.index("docker builder prune"),
                        self.workflow.index("Warm egress image"))
        self.assertLess(self.workflow.index("Warm egress image"),
                        self.workflow.index("- name: Exercise Docker integration"))
        self.assertIn("if: steps.ci_paths.outputs.run_heavy == 'true'", self.step)
        self.assertIn("timeout-minutes: 10", self.step)
        self.assertNotIn("continue-on-error", self.step)

    def test_warm_build_uses_smoke_build_inputs_not_smoke_project_tags(self):
        self.assertIn(
            "run: docker build -t irlight-egress-ci-warm -f "
            "apps/egress-gateway/Dockerfile apps/egress-gateway", self.step
        )
        for name in ("reconnect", "stop-terminal", "dns-tls", "publish-conflict"):
            source = (ROOT / f"scripts/smoke-egress-{name}.sh").read_text()
            blocks = re.findall(
                r"    build:\n((?:      [^\n]*\n)+)", source
            )
            egress = [b for b in blocks if "context: ./apps/egress-gateway\n" in b]
            self.assertTrue(egress, name)
            for block in egress:
                self.assertEqual(
                    set(line.strip() for line in block.splitlines()),
                    {"context: ./apps/egress-gateway", "dockerfile: Dockerfile"},
                )
            # Per-run Compose tags/isolation stay intact, not a global image
            # substituted in place of building the checked-out sources.
            self.assertNotIn("image: irlight-egress-ci-warm", source)
            self.assertNotIn("--no-cache", source)

    def test_warm_build_failure_is_not_masked(self):
        command = re.search(r"^        run: (.+)$", self.step, re.MULTILINE)[1]
        with tempfile.TemporaryDirectory() as tmp:
            docker = Path(tmp) / "docker"
            docker.write_text("#!/bin/sh\nexit 37\n")
            docker.chmod(0o755)
            result = subprocess.run(
                ["bash", "-e", "-c", command],
                env={**os.environ, "PATH": tmp + os.pathsep + os.environ["PATH"]},
                check=False,
            )
            self.assertEqual(result.returncode, 37)

    def test_scenario_budget_and_rebuilds_remain_enforced(self):
        suite = (ROOT / "scripts/ci-docker-smoke-suite.sh").read_text()
        self.assertIn("smoke_timeout_seconds=180", suite)
        self.assertIn('status=${PIPESTATUS[0]}', suite)
        for name in ("reconnect", "stop-terminal"):
            source = (ROOT / f"scripts/smoke-egress-{name}.sh").read_text()
            self.assertIn('"${compose[@]}" up -d --build', source)


if __name__ == "__main__":
    unittest.main()
