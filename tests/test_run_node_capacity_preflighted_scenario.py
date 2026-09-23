from __future__ import annotations

import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run-node-capacity-preflighted-scenario.py"
SPEC = importlib.util.spec_from_file_location("node_capacity_preflighted_runner_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class FakeHostPreflightError(RuntimeError):
    pass


class NodeCapacityPreflightedScenarioTests(unittest.TestCase):
    def _ready_snapshot(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "kind": "irlight-node-capacity-host-preflight",
            "ready": True,
            "platform": {},
            "resources": {},
            "docker": {},
        }

    def test_successful_preflight_runs_existing_runner_with_unchanged_arguments(self) -> None:
        events: list[object] = []
        argv = ["--plan", "plan.json", "--scenario", "normal-input"]

        def collect_snapshot() -> dict[str, object]:
            events.append("preflight")
            return self._ready_snapshot()

        def runner_main(received: list[str] | None) -> int:
            events.append(("runner", received))
            return 0

        preflight = types.SimpleNamespace(
            HostPreflightError=FakeHostPreflightError,
            collect_snapshot=collect_snapshot,
        )
        runner = types.SimpleNamespace(main=runner_main)

        result = MODULE.run_preflighted(
            argv,
            preflight_module=preflight,
            runner_module=runner,
        )

        self.assertEqual(result, 0)
        self.assertEqual(events, ["preflight", ("runner", argv)])

    def test_optional_preflight_evidence_is_written_before_runner_and_option_is_stripped(self) -> None:
        snapshot = self._ready_snapshot()
        events: list[object] = []
        preflight = types.SimpleNamespace(
            HostPreflightError=FakeHostPreflightError,
            collect_snapshot=lambda: snapshot,
        )

        with tempfile.TemporaryDirectory() as tmp:
            evidence = pathlib.Path(tmp) / "host-preflight.json"

            def runner_main(received: list[str] | None) -> int:
                events.append(("evidence_exists", evidence.exists()))
                events.append(("runner", received))
                return 0

            runner = types.SimpleNamespace(main=runner_main)
            result = MODULE.run_preflighted(
                [
                    "--plan",
                    "plan.json",
                    "--preflight-json",
                    str(evidence),
                    "--scenario",
                    "normal-input",
                ],
                preflight_module=preflight,
                runner_module=runner,
            )

            self.assertEqual(result, 0)
            self.assertEqual(
                events,
                [
                    ("evidence_exists", True),
                    ("runner", ["--plan", "plan.json", "--scenario", "normal-input"]),
                ],
            )
            self.assertEqual(json.loads(evidence.read_text(encoding="utf-8")), snapshot)
            self.assertEqual(evidence.stat().st_mode & 0o777, 0o600)

    def test_existing_preflight_evidence_fails_closed_before_runner(self) -> None:
        runner_called = False
        preflight = types.SimpleNamespace(
            HostPreflightError=FakeHostPreflightError,
            collect_snapshot=self._ready_snapshot,
        )

        def runner_main(received: list[str] | None) -> int:
            nonlocal runner_called
            runner_called = True
            return 0

        runner = types.SimpleNamespace(main=runner_main)
        with tempfile.TemporaryDirectory() as tmp:
            evidence = pathlib.Path(tmp) / "host-preflight.json"
            evidence.write_text("do-not-overwrite\n", encoding="utf-8")
            with self.assertRaisesRegex(
                MODULE.PreflightedScenarioError,
                "local host preflight evidence could not be published",
            ) as context:
                MODULE.run_preflighted(
                    ["--preflight-json", str(evidence), "--plan", "plan.json"],
                    preflight_module=preflight,
                    runner_module=runner,
                )

            self.assertFalse(runner_called)
            self.assertEqual(evidence.read_text(encoding="utf-8"), "do-not-overwrite\n")
            self.assertNotIn(str(evidence), str(context.exception))

    def test_invalid_wrapper_evidence_option_fails_before_preflight_or_runner(self) -> None:
        preflight_called = False
        runner_called = False

        def collect_snapshot() -> dict[str, object]:
            nonlocal preflight_called
            preflight_called = True
            return self._ready_snapshot()

        def runner_main(received: list[str] | None) -> int:
            nonlocal runner_called
            runner_called = True
            return 0

        preflight = types.SimpleNamespace(
            HostPreflightError=FakeHostPreflightError,
            collect_snapshot=collect_snapshot,
        )
        runner = types.SimpleNamespace(main=runner_main)

        for argv in (["--preflight-json"], ["--preflight-json="], ["--preflight-json", "a", "--preflight-json", "b"]):
            with self.subTest(argv=argv):
                with self.assertRaises(MODULE.PreflightedScenarioError):
                    MODULE.run_preflighted(
                        argv,
                        preflight_module=preflight,
                        runner_module=runner,
                    )
        self.assertFalse(preflight_called)
        self.assertFalse(runner_called)

    def test_failed_preflight_never_starts_runner(self) -> None:
        runner_called = False

        def collect_snapshot() -> dict[str, object]:
            raise FakeHostPreflightError("ssh://sensitive-host.example.invalid")

        def runner_main(received: list[str] | None) -> int:
            nonlocal runner_called
            runner_called = True
            return 0

        preflight = types.SimpleNamespace(
            HostPreflightError=FakeHostPreflightError,
            collect_snapshot=collect_snapshot,
        )
        runner = types.SimpleNamespace(main=runner_main)

        with self.assertRaisesRegex(
            MODULE.PreflightedScenarioError,
            "local host prerequisite check failed",
        ) as context:
            MODULE.run_preflighted(
                ["--runner", "must-not-run"],
                preflight_module=preflight,
                runner_module=runner,
            )

        self.assertFalse(runner_called)
        self.assertNotIn("sensitive-host", str(context.exception))

    def test_malformed_or_non_ready_snapshot_fails_before_runner(self) -> None:
        invalid_snapshots = (
            None,
            {},
            {"schema_version": 2, "kind": "irlight-node-capacity-host-preflight", "ready": True},
            {"schema_version": 1, "kind": "unexpected", "ready": True},
            {"schema_version": 1, "kind": "irlight-node-capacity-host-preflight", "ready": False},
        )
        for snapshot in invalid_snapshots:
            with self.subTest(snapshot=snapshot):
                runner_called = False

                def runner_main(received: list[str] | None) -> int:
                    nonlocal runner_called
                    runner_called = True
                    return 0

                preflight = types.SimpleNamespace(
                    HostPreflightError=FakeHostPreflightError,
                    collect_snapshot=lambda snapshot=snapshot: snapshot,
                )
                runner = types.SimpleNamespace(main=runner_main)
                with self.assertRaises(MODULE.PreflightedScenarioError):
                    MODULE.run_preflighted(
                        [],
                        preflight_module=preflight,
                        runner_module=runner,
                    )
                self.assertFalse(runner_called)

    def test_runner_exit_code_is_preserved_after_preflight(self) -> None:
        preflight = types.SimpleNamespace(
            HostPreflightError=FakeHostPreflightError,
            collect_snapshot=self._ready_snapshot,
        )
        runner = types.SimpleNamespace(main=lambda argv: 2)
        self.assertEqual(
            MODULE.run_preflighted([], preflight_module=preflight, runner_module=runner),
            2,
        )

    def test_main_prints_only_bounded_wrapper_error(self) -> None:
        stderr = io.StringIO()
        with mock.patch.object(
            MODULE,
            "run_preflighted",
            side_effect=MODULE.PreflightedScenarioError("local host prerequisite check failed"),
        ), redirect_stderr(stderr):
            result = MODULE.main(["--runner", "unused"])

        self.assertEqual(result, 2)
        self.assertEqual(
            stderr.getvalue(),
            "node capacity scenario preflight failed: local host prerequisite check failed\n",
        )


if __name__ == "__main__":
    unittest.main()
