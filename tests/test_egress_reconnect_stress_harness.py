from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "stress-egress-reconnect.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "egress-reconnect-stress.yml"
_STRESS_ENV_KEYS = (
    "IRLIGHT_EGRESS_RECONNECT_STRESS_RUNS",
    "IRLIGHT_EGRESS_RECONNECT_STRESS_SCENARIO",
)


class EgressReconnectStressHarnessTest(unittest.TestCase):
    def _run(self, *, env: dict[str, str] | None = None, args: list[str] | None = None):
        run_env = os.environ.copy()
        for key in _STRESS_ENV_KEYS:
            run_env.pop(key, None)
        if env:
            run_env.update(env)
        return subprocess.run(
            ["bash", str(SCRIPT), *(args or [])],
            cwd=ROOT,
            env=run_env,
            check=False,
            capture_output=True,
            text=True,
        )

    def test_help_is_side_effect_free(self) -> None:
        result = self._run(args=["--help"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("intentionally not part of the regular CI smoke suite", result.stdout)
        self.assertNotIn("docker", result.stderr.lower())

    def test_invalid_runs_fail_before_any_smoke(self) -> None:
        for value in ("0", "51", "abc", "1.5", "-1"):
            with self.subTest(value=value):
                result = self._run(
                    env={"IRLIGHT_EGRESS_RECONNECT_STRESS_RUNS": value}
                )
                self.assertEqual(result.returncode, 2)
                self.assertEqual(
                    result.stderr,
                    "IRLIGHT_EGRESS_STRESS status=ERROR reason=invalid_runs\n",
                )
                self.assertEqual(result.stdout, "")

    def test_invalid_scenario_fails_before_any_smoke(self) -> None:
        result = self._run(
            env={"IRLIGHT_EGRESS_RECONNECT_STRESS_SCENARIO": "unknown"}
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(
            result.stderr,
            "IRLIGHT_EGRESS_STRESS status=ERROR reason=invalid_scenario\n",
        )
        self.assertEqual(result.stdout, "")

    def test_regular_ci_does_not_run_the_stress_harness(self) -> None:
        suite = (ROOT / "scripts" / "ci-docker-smoke-suite.sh").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("stress-egress-reconnect.sh", suite)

    def test_harness_delegates_to_existing_secret_safe_smokes(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("run_smoke reconnect smoke-egress-reconnect.sh", source)
        self.assertIn("run_smoke stop-terminal smoke-egress-stop-terminal.sh", source)
        self.assertNotIn("docker compose", source)
        self.assertNotIn("stream_key", source)

    def test_failure_path_retains_only_bounded_stack_fingerprint(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("umask 077", source)
        self.assertIn("extract-egress-stack-fingerprint.py", source)
        self.assertIn(
            "IRLIGHT_EGRESS_STACK_FINGERPRINT stack_fingerprint=UNAVAILABLE capped=yes",
            source,
        )
        self.assertNotIn(
            "IRLIGHT_EGRESS_STACK_FINGERPRINT stack_fingerprint=UNAVAILABLE capped=no",
            source,
        )
        self.assertIn("GITHUB_STEP_SUMMARY", source)
        self.assertIn("tee \"$log_file\"", source)
        self.assertNotIn("upload-artifact", source)

    def test_manual_workflow_is_opt_in_read_only_and_bounded(self) -> None:
        source = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", source)
        self.assertNotIn("pull_request:", source)
        self.assertNotIn("schedule:", source)
        self.assertIn("permissions:\n  contents: read", source)
        self.assertIn("group: egress-reconnect-stress", source)
        self.assertIn("timeout-minutes: 45", source)
        self.assertIn('default: "3"', source)
        self.assertIn('          - "1"\n          - "3"\n          - "5"', source)
        self.assertIn(
            "IRLIGHT_EGRESS_RECONNECT_STRESS_RUNS: ${{ inputs.runs }}", source
        )
        self.assertIn(
            "IRLIGHT_EGRESS_RECONNECT_STRESS_SCENARIO: ${{ inputs.scenario }}",
            source,
        )
        self.assertIn("bash ./scripts/stress-egress-reconnect.sh", source)
        self.assertNotIn("secrets.", source)

    def test_manual_workflow_uploads_only_safe_failure_summary(self) -> None:
        source = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("id: egress_stress", source)
        self.assertIn(
            "failure() && steps.egress_stress.outcome == 'failure'", source
        )
        self.assertIn(
            "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
            source,
        )
        self.assertIn('cp -- "$GITHUB_STEP_SUMMARY" "$evidence_dir/summary.md"', source)
        self.assertIn("retention-days: 3", source)
        self.assertNotIn("docker logs", source)
        self.assertNotIn("/tmp/irlight-egress", source)


if __name__ == "__main__":
    unittest.main()
