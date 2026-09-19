from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


class CiFailureArtifactContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = WORKFLOW.read_text(encoding="utf-8")

    def test_docker_smoke_failure_artifact_retains_only_safe_step_summary(self) -> None:
        self.assertIn("id: docker_smoke_suite", self.source)
        self.assertIn(
            'evidence_dir="$RUNNER_TEMP/irlight-docker-smoke-diagnostics"',
            self.source,
        )
        self.assertIn(
            'cp -- "$GITHUB_STEP_SUMMARY" "$evidence_dir/summary.md"',
            self.source,
        )
        self.assertIn(
            "if: ${{ failure() && steps.docker_smoke_suite.outcome == 'failure' }}",
            self.source,
        )
        self.assertIn(
            "name: docker-smoke-failure-evidence-${{ github.run_id }}-${{ github.run_attempt }}",
            self.source,
        )
        self.assertIn(
            "path: ${{ runner.temp }}/irlight-docker-smoke-diagnostics",
            self.source,
        )
        self.assertIn("retention-days: 3", self.source)

        docker_artifact_block = self.source.split(
            "- name: Retain Docker smoke failure evidence", 1
        )[1].split("- name: Exercise measured soak evidence chain", 1)[0]
        self.assertNotIn("*.log", docker_artifact_block)
        self.assertNotIn("docker inspect", docker_artifact_block)
        self.assertNotIn("scenario_log", docker_artifact_block)

    def test_measured_soak_artifact_only_runs_when_measured_soak_failed(self) -> None:
        self.assertIn("id: measured_soak", self.source)
        self.assertIn(
            "if: ${{ failure() && steps.measured_soak.outcome == 'failure' }}",
            self.source,
        )
        self.assertIn(
            "name: measured-soak-failure-evidence-${{ github.run_id }}-${{ github.run_attempt }}",
            self.source,
        )

    def test_docker_smoke_failure_copy_preserves_original_exit_status(self) -> None:
        smoke_step = self.source.split(
            "- name: Exercise Docker integration smoke suite", 1
        )[1].split("- name: Retain Docker smoke failure evidence", 1)[0]
        self.assertIn(
            "bash ./scripts/ci-docker-smoke-suite.sh || smoke_status=$?",
            smoke_step,
        )
        self.assertIn('exit "$smoke_status"', smoke_step)
        self.assertLess(
            smoke_step.index('cp -- "$GITHUB_STEP_SUMMARY"'),
            smoke_step.index('exit "$smoke_status"'),
        )


if __name__ == "__main__":
    unittest.main()
