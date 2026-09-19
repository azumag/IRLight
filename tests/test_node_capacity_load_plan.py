from __future__ import annotations

import importlib.util
import io
import json
import pathlib
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "render-node-capacity-load-plan.py"
SPEC = importlib.util.spec_from_file_location("node_capacity_load_plan", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class NodeCapacityLoadPlanTests(unittest.TestCase):
    def test_default_plan_covers_issue_13_baseline_scenarios(self) -> None:
        plan = MODULE.build_plan("720p30 3Mbps")
        self.assertEqual(plan["schema_version"], 1)
        self.assertEqual(plan["source_issue"], 13)
        self.assertEqual(plan["session_counts"], [1, 2, 4, 8])
        self.assertEqual(
            [case["id"] for case in plan["scenarios"]],
            [
                "normal-input",
                "all-holding",
                "reconnect-storm",
                "asset-prefetch",
                "api-dashboard",
            ],
        )
        for case in plan["scenarios"]:
            self.assertEqual(case["session_counts"], [1, 2, 4, 8])
        self.assertEqual(plan["execution"], "plan-only")

    def test_explicit_levels_extend_without_replacing_baseline(self) -> None:
        plan = MODULE.build_plan("explicit mixed profile", [16, 4, 32, 16])
        self.assertEqual(plan["session_counts"], [1, 2, 4, 8, 16, 32])

    def test_programmatic_invalid_counts_fail_closed(self) -> None:
        for value in (0, -1, True, 1.5, "16"):
            with self.subTest(value=value):
                with self.assertRaises(MODULE.PlanError):
                    MODULE.normalize_session_counts([value])

    def test_profile_label_must_be_one_nonempty_line(self) -> None:
        for value in ("", "   ", "a\nb", "a\rb", "a\x00b"):
            with self.subTest(value=value):
                with self.assertRaises(MODULE.PlanError):
                    MODULE.normalize_profile_label(value)

    def test_cli_emits_deterministic_json_and_rejects_nonpositive_level(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(
                MODULE.main(["--profile-label", "1080p30 5Mbps", "--session-count", "16"]),
                0,
            )
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["session_counts"], [1, 2, 4, 8, 16])
        self.assertEqual(payload["profile_label"], "1080p30 5Mbps")

        stderr = io.StringIO()
        with self.assertRaises(SystemExit) as ctx, redirect_stderr(stderr):
            MODULE.parse_args(["--profile-label", "720p30", "--session-count", "0"])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("greater than zero", stderr.getvalue())

    def test_script_has_no_load_execution_or_network_imports(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        for forbidden in (
            "import subprocess",
            "from subprocess",
            "import socket",
            "from socket",
            "import requests",
            "import urllib",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
