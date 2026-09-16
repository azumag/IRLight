from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import pathlib
import sys
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "network-fault-case-runner.py"
SPEC = importlib.util.spec_from_file_location("network_fault_case_runner", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)


class NetworkFaultCaseRunnerTest(unittest.TestCase):
    def _select(self, case_id: str) -> dict[str, object]:
        return runner.select_case(
            case_id=case_id,
            namespace="irlight-qa",
            interface="eth0",
        )

    def _execute(self, case: dict[str, object], **kwargs: object) -> int:
        kwargs.setdefault("confirm_disposable_namespace", True)
        return runner.execute_case(case, **kwargs)

    def test_select_case_uses_stable_matrix_id_and_namespaced_commands(self) -> None:
        case = self._select("rtmp-loss-1pct-30s")
        self.assertEqual(case["id"], "rtmp-loss-1pct-30s")
        plan = case["plan"]
        self.assertEqual(
            plan["apply_argv"][:4],
            ["ip", "netns", "exec", "irlight-qa"],
        )
        self.assertEqual(
            plan["cleanup_argv"][:4],
            ["ip", "netns", "exec", "irlight-qa"],
        )

    def test_unknown_case_is_rejected_without_execution(self) -> None:
        with mock.patch.object(runner, "_run_command") as run_command:
            with self.assertRaisesRegex(runner.CaseRunnerError, "not in the matrix"):
                self._select("rtmp-loss-2pct-30s")
        run_command.assert_not_called()

    def test_execute_case_applies_waits_and_cleans_up(self) -> None:
        case = self._select("srt-latency-100ms-30s")
        plan = case["plan"]
        with mock.patch.object(runner, "_run_command") as run_command:
            with mock.patch.object(runner.time, "sleep") as sleep:
                result = self._execute(case)

        self.assertEqual(result, 0)
        self.assertEqual(
            run_command.call_args_list,
            [mock.call(plan["apply_argv"]), mock.call(plan["cleanup_argv"])],
        )
        sleep.assert_called_once_with(30)

    def test_execute_case_cleans_up_after_keyboard_interrupt(self) -> None:
        case = self._select("rtmp-disconnect-10s")
        plan = case["plan"]
        with mock.patch.object(runner, "_run_command") as run_command:
            with mock.patch.object(runner.time, "sleep", side_effect=KeyboardInterrupt):
                result = self._execute(case)

        self.assertEqual(result, 130)
        self.assertEqual(
            run_command.call_args_list,
            [mock.call(plan["apply_argv"]), mock.call(plan["cleanup_argv"])],
        )

    def test_apply_failure_still_attempts_cleanup(self) -> None:
        case = self._select("rtmp-loss-1pct-30s")
        plan = case["plan"]
        with mock.patch.object(
            runner,
            "_run_command",
            side_effect=[runner.CaseRunnerError("apply command failed"), None],
        ) as run_command:
            with mock.patch.object(runner.time, "sleep") as sleep:
                with self.assertRaisesRegex(runner.CaseRunnerError, "apply failed"):
                    self._execute(case)

        self.assertEqual(
            run_command.call_args_list,
            [mock.call(plan["apply_argv"]), mock.call(plan["cleanup_argv"])],
        )
        sleep.assert_not_called()

    def test_interrupt_during_apply_still_attempts_cleanup(self) -> None:
        case = self._select("rtmp-loss-1pct-30s")
        plan = case["plan"]
        with mock.patch.object(
            runner,
            "_run_command",
            side_effect=[KeyboardInterrupt(), None],
        ) as run_command:
            with mock.patch.object(runner.time, "sleep") as sleep:
                result = self._execute(case)

        self.assertEqual(result, 130)
        self.assertEqual(
            run_command.call_args_list,
            [mock.call(plan["apply_argv"]), mock.call(plan["cleanup_argv"])],
        )
        sleep.assert_not_called()

    def test_cleanup_failure_remains_visible(self) -> None:
        case = self._select("rtmp-disconnect-10s")
        with mock.patch.object(
            runner,
            "_run_command",
            side_effect=[None, runner.CaseRunnerError("cleanup command failed")],
        ):
            with mock.patch.object(runner.time, "sleep"):
                with self.assertRaisesRegex(runner.CaseRunnerError, "cleanup failed"):
                    self._execute(case)

    def test_apply_and_cleanup_failure_reports_uncertain_cleanup(self) -> None:
        case = self._select("rtmp-disconnect-10s")
        with mock.patch.object(
            runner,
            "_run_command",
            side_effect=[
                runner.CaseRunnerError("apply command failed"),
                runner.CaseRunnerError("cleanup command failed"),
            ],
        ):
            with self.assertRaisesRegex(
                runner.CaseRunnerError, "cleanup could not be confirmed"
            ):
                self._execute(case)

    def test_plan_mode_is_read_only(self) -> None:
        stdout = io.StringIO()
        with mock.patch.object(runner, "_run_command") as run_command:
            with contextlib.redirect_stdout(stdout):
                result = runner.main(
                    [
                        "plan",
                        "--case",
                        "rtmp-loss-3pct-30s",
                        "--namespace",
                        "irlight-qa",
                        "--interface",
                        "eth0",
                    ]
                )

        self.assertEqual(result, 0)
        run_command.assert_not_called()
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["id"], "rtmp-loss-3pct-30s")

    def test_apply_mode_requires_disposable_namespace_acknowledgement(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            runner.main(
                [
                    "apply",
                    "--case",
                    "rtmp-loss-1pct-30s",
                    "--namespace",
                    "irlight-qa",
                    "--interface",
                    "eth0",
                ]
            )
        self.assertEqual(raised.exception.code, 2)

    def test_programmatic_execution_requires_disposable_namespace_acknowledgement(self) -> None:
        case = self._select("rtmp-loss-1pct-30s")
        with mock.patch.object(runner, "_run_command") as run_command:
            with self.assertRaisesRegex(
                runner.CaseRunnerError, "acknowledgement is required"
            ):
                runner.execute_case(case)
        run_command.assert_not_called()

    def test_invalid_execution_shape_is_rejected_before_command(self) -> None:
        case = self._select("rtmp-loss-1pct-30s")
        case["fault"] = {"duration_seconds": True}
        with mock.patch.object(runner, "_run_command") as run_command:
            with self.assertRaisesRegex(runner.CaseRunnerError, "invalid execution data"):
                self._execute(case)
        run_command.assert_not_called()

    def test_tampered_apply_argv_is_rejected_before_command(self) -> None:
        case = self._select("rtmp-loss-1pct-30s")
        case["plan"]["apply_argv"] = ["echo", "not-a-generated-command"]
        with mock.patch.object(runner, "_run_command") as run_command:
            with self.assertRaisesRegex(
                runner.CaseRunnerError, "does not match generated matrix"
            ):
                self._execute(case)
        run_command.assert_not_called()

    def test_extra_case_fields_are_rejected_before_command(self) -> None:
        case = self._select("rtmp-loss-1pct-30s")
        case["unexpected"] = "not part of the matrix schema"
        with mock.patch.object(runner, "_run_command") as run_command:
            with self.assertRaisesRegex(
                runner.CaseRunnerError, "does not match generated matrix"
            ):
                self._execute(case)
        run_command.assert_not_called()

    def test_loopback_execution_requires_programmatic_acknowledgement(self) -> None:
        case = runner.select_case(
            case_id="rtmp-loss-1pct-30s",
            namespace="irlight-qa",
            interface="lo",
            allow_loopback=True,
        )
        with mock.patch.object(runner, "_run_command") as run_command:
            with self.assertRaisesRegex(
                runner.CaseRunnerError, "could not be regenerated safely"
            ):
                self._execute(case)
        run_command.assert_not_called()

        with mock.patch.object(runner, "_run_command") as run_command:
            with mock.patch.object(runner.time, "sleep"):
                self.assertEqual(self._execute(case, allow_loopback=True), 0)
        self.assertEqual(run_command.call_count, 2)


if __name__ == "__main__":
    unittest.main()
