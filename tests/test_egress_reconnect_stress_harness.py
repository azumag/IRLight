from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "stress-egress-reconnect.sh"


class EgressReconnectStressHarnessTest(unittest.TestCase):
    def _run(self, *, env: dict[str, str] | None = None, args: list[str] | None = None):
        run_env = os.environ.copy()
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


if __name__ == "__main__":
    unittest.main()
