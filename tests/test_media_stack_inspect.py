from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "node-agent"))

from media_stack_inspect_cli import (  # noqa: E402
    MediaStackInspectError,
    expected_services,
    inspect_media_stack,
    main as media_stack_inspect_main,
    parse_compose_ps,
)


def completed(command: list[str], stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, stdout, "")


def ps_row(service: str, name: str, *, health: str = "healthy") -> dict[str, object]:
    return {
        "Service": service,
        "Name": name,
        "State": "running",
        "Health": health,
        # Secret-looking fields must never be forwarded by the inspector.
        "Command": "run --token super-secret-value",
        "Publishers": [{"URL": "rtmp://example.invalid/secret-key"}],
    }


class ComposePsParsingTest(unittest.TestCase):
    def test_accepts_array_and_line_delimited_json(self) -> None:
        rows = [ps_row("mediamtx", "node-mediamtx-1"), ps_row("continuity", "node-continuity-1")]
        self.assertEqual(parse_compose_ps(json.dumps(rows)), rows)
        self.assertEqual(parse_compose_ps("\n".join(json.dumps(row) for row in rows)), rows)

    def test_rejects_invalid_json_shape(self) -> None:
        with self.assertRaises(MediaStackInspectError):
            parse_compose_ps('{"Service":"mediamtx"}')
        with self.assertRaises(MediaStackInspectError):
            parse_compose_ps("not-json")


class MediaStackInspectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="irlight-media-inspect-")
        self.compose_file = Path(self.tmp.name) / "docker-compose.control.yml"
        self.compose_file.write_text("services: {}\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _inspect(
        self,
        rows: list[dict[str, object]],
        states: list[str],
        *,
        restart_warning_count: int = 3,
    ) -> tuple[dict[str, object], list[list[str]]]:
        calls: list[list[str]] = []
        responses = [completed(["docker", "compose"], json.dumps(rows))]
        responses.extend(completed(["docker", "inspect"], state) for state in states)

        def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return responses.pop(0)

        with patch("media_stack_inspect_cli._run_readonly", side_effect=fake_run):
            payload = inspect_media_stack(
                compose_file=self.compose_file,
                project_name="irlight-node",
                timeout_seconds=5.0,
                restart_warning_count=restart_warning_count,
            )
        self.assertFalse(responses)
        return payload, calls

    @patch.dict(os.environ, {"NODE_EGRESS_MODE": "DIRECT_PUSH", "EGRESS_GATEWAY_ENABLED": "1"})
    def test_healthy_stack_is_ok_and_output_is_redacted(self) -> None:
        rows = [
            ps_row("mediamtx", "node-mediamtx-1"),
            ps_row("continuity", "node-continuity-1"),
            ps_row("egress-gateway", "node-egress-1", health=""),
        ]
        payload, calls = self._inspect(
            rows,
            ["running\t0\tfalse\t0\n", "running\t0\tfalse\t0\n", "running\t0\tfalse\t0\n"],
        )

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["problem_count"], 0)
        self.assertEqual(payload["warning_count"], 0)
        rendered = json.dumps(payload)
        self.assertNotIn("super-secret-value", rendered)
        self.assertNotIn("secret-key", rendered)
        self.assertNotIn("node-mediamtx-1", rendered)

        self.assertIn("ps", calls[0])
        self.assertNotIn("up", calls[0])
        self.assertNotIn("start", calls[0])
        self.assertNotIn("stop", calls[0])
        for command in calls[1:]:
            self.assertEqual(command[:3], ["docker", "inspect", "--format"])
            self.assertIn("{{.State.Status}}", command[3])
            self.assertNotIn("{{json .}}", command[3])

    @patch.dict(os.environ, {"NODE_EGRESS_MODE": "DIRECT_PUSH", "EGRESS_GATEWAY_ENABLED": "1"})
    def test_restarting_oom_and_unhealthy_are_problems(self) -> None:
        rows = [
            ps_row("mediamtx", "node-mediamtx-1"),
            ps_row("continuity", "node-continuity-1", health="unhealthy"),
            ps_row("egress-gateway", "node-egress-1", health=""),
        ]
        payload, _calls = self._inspect(
            rows,
            ["restarting\t137\ttrue\t7\n", "running\t0\tfalse\t0\n", "exited\t2\tfalse\t0\n"],
        )

        self.assertEqual(payload["status"], "PROBLEM")
        by_service = {item["service"]: item for item in payload["services"]}
        self.assertIn("state_restarting", by_service["mediamtx"]["problems"])
        self.assertIn("oom_killed", by_service["mediamtx"]["problems"])
        self.assertIn("restart_history_high", by_service["mediamtx"]["warnings"])
        self.assertIn("unhealthy", by_service["continuity"]["problems"])
        self.assertIn("not_running", by_service["egress-gateway"]["problems"])

    @patch.dict(os.environ, {"NODE_EGRESS_MODE": "DIRECT_PUSH", "EGRESS_GATEWAY_ENABLED": "1"})
    def test_missing_expected_service_is_problem_without_inspect_call(self) -> None:
        rows = [ps_row("mediamtx", "node-mediamtx-1"), ps_row("continuity", "node-continuity-1")]
        payload, calls = self._inspect(
            rows,
            ["running\t0\tfalse\t0\n", "running\t0\tfalse\t0\n"],
        )

        self.assertEqual(payload["status"], "PROBLEM")
        egress = next(item for item in payload["services"] if item["service"] == "egress-gateway")
        self.assertEqual(egress["problems"], ["missing"])
        self.assertEqual(len(calls), 3)

    @patch.dict(os.environ, {"NODE_EGRESS_MODE": "DIRECT_PUSH", "EGRESS_GATEWAY_ENABLED": "1"})
    def test_elevated_lifetime_restart_count_is_warning_not_crash_loop_proof(self) -> None:
        rows = [
            ps_row("mediamtx", "node-mediamtx-1"),
            ps_row("continuity", "node-continuity-1"),
            ps_row("egress-gateway", "node-egress-1", health=""),
        ]
        payload, _calls = self._inspect(
            rows,
            ["running\t0\tfalse\t8\n", "running\t0\tfalse\t0\n", "running\t0\tfalse\t0\n"],
        )

        self.assertEqual(payload["status"], "WARNING")
        self.assertEqual(payload["problem_count"], 0)
        self.assertEqual(payload["warning_count"], 1)

    def test_relay_only_does_not_expect_egress_gateway(self) -> None:
        with patch.dict(os.environ, {"NODE_EGRESS_MODE": "RELAY_ONLY", "EGRESS_GATEWAY_ENABLED": "1"}):
            self.assertEqual(expected_services(), ("mediamtx", "continuity"))
            rows = [ps_row("mediamtx", "node-mediamtx-1"), ps_row("continuity", "node-continuity-1")]
            payload, _calls = self._inspect(
                rows,
                ["running\t0\tfalse\t0\n", "running\t0\tfalse\t0\n"],
            )
        self.assertEqual(payload["status"], "OK")
        self.assertEqual([item["service"] for item in payload["services"]], ["mediamtx", "continuity"])

    @patch.dict(os.environ, {"NODE_EGRESS_MODE": "DIRECT_PUSH", "EGRESS_GATEWAY_ENABLED": "0"})
    def test_disabled_gateway_is_not_expected(self) -> None:
        self.assertEqual(expected_services(), ("mediamtx", "continuity"))

    @patch.dict(os.environ, {"NODE_EGRESS_MODE": "DIRECT_PUSH", "EGRESS_GATEWAY_ENABLED": "1"})
    def test_inspect_failure_fails_closed_without_echoing_stderr(self) -> None:
        rows = [
            ps_row("mediamtx", "node-mediamtx-1"),
            ps_row("continuity", "node-continuity-1"),
            ps_row("egress-gateway", "node-egress-1"),
        ]
        responses = [
            completed(["docker", "compose"], json.dumps(rows)),
            subprocess.CompletedProcess(["docker", "inspect"], 1, "", "secret-from-docker"),
        ]
        with patch("media_stack_inspect_cli._run_readonly", side_effect=responses):
            with self.assertRaises(MediaStackInspectError) as raised:
                inspect_media_stack(
                    compose_file=self.compose_file,
                    project_name="irlight-node",
                    timeout_seconds=5.0,
                    restart_warning_count=3,
                )
        self.assertNotIn("secret-from-docker", str(raised.exception))


class MediaStackInspectCliTest(unittest.TestCase):
    def test_unavailable_cli_is_generic_and_non_secret(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = media_stack_inspect_main(["--compose-file", "/definitely/missing.yml"])

        self.assertEqual(result, 3)
        self.assertEqual(
            json.loads(output.getvalue()),
            {"reason": "media stack inspection unavailable", "status": "UNAVAILABLE"},
        )

    def test_invalid_env_threshold_is_argument_error(self) -> None:
        stderr = io.StringIO()
        with patch.dict(os.environ, {"NODE_RESTART_WARNING_COUNT": "0"}):
            with contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as raised:
                    media_stack_inspect_main(["--compose-file", "/tmp/missing.yml"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("positive integer", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
