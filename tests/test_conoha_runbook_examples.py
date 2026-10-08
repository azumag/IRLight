"""Execute runbook examples with strict command stubs; never use real services."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "docs" / "conoha-runtime-verification.md"
SUCCESS = "STATE_DIR は初期化済み"

# This is the only sudo on PATH. Unexpected commands fail instead of reaching
# systemd, Docker, the network, or any provider credentials.
SUDO_STUB = r'''
import json, os, sys
from pathlib import Path
root = Path(os.environ["STUB_ROOT"])
config = json.loads((root / "config.json").read_text())
args = sys.argv[1:]
with (root / "calls.jsonl").open("a") as out:
    out.write(json.dumps(args) + "\n")
if args[:2] == ["docker", "compose"]:
    key = "docker"
elif args[:2] == ["systemctl", "start"]:
    assert args == ["systemctl", "start", "irlight-reaper.service"]
    key = "start"
elif args[:2] == ["systemctl", "show"]:
    assert args[2] == "irlight-reaper.service"
    if "--value" in args:
        key = "before"
    else:
        counter = root / "show-count"
        n = int(counter.read_text()) if counter.exists() else 0
        counter.write_text(str(n + 1))
        key = "state" if n == 0 else "state_after"
elif args[:2] == ["journalctl", "-n"]:
    assert args == ["journalctl", "-n", "1", "-o", "json", "--no-pager"]
    key = "cursor"
elif args and args[0] == "journalctl":
    assert args == ["journalctl", "-b", "-u", "irlight-reaper.service",
                    "--after-cursor=synthetic-cursor", "-o", "json", "--no-pager"]
    key = "journal"
else:
    raise SystemExit("unexpected command in runbook")
reply = config[key]
sys.stdout.write(reply.get("stdout", ""))
sys.stderr.write(reply.get("stderr", ""))
raise SystemExit(reply.get("rc", 0))
'''

CURL_STUB = r'''
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
assert args[-2:] == ["--data-binary", "@-"]
assert args[args.index("-X") + 1] == "POST"
assert "https://control.invalid/v1/auth/login" in args
Path(os.environ["CAPTURE"]).write_text(json.dumps({"args": args, "payload": sys.stdin.read()}))
raise SystemExit(int(os.environ.get("CURL_RC", "0")))
'''


def _entry(message: str, timestamp: int = 250) -> str:
    return json.dumps({"__MONOTONIC_TIMESTAMP": str(timestamp), "MESSAGE": message}) + "\n"


def _summary(**overrides: object) -> str:
    return repr({"orphan_cleanup": 0, "auth_session_gc_status": "ok", **overrides})


class ConohaRunbookExamplesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runbook = RUNBOOK.read_text(encoding="utf-8")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        # Use exactly the current test interpreter for the runbook subprocess.
        self._stub("python3", 'import os, sys\nos.execv(sys.executable, [sys.executable, "-S", *sys.argv[1:]])\n')
        self.env = {"PATH": str(self.bin) + os.pathsep + os.defpath,
                    "STUB_ROOT": str(self.root), "HOME": str(self.root),
                    "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
        self._stub("sudo", SUDO_STUB)
        self._stub("curl", CURL_STUB)

    def _stub(self, name: str, source: str) -> None:
        path = self.bin / name
        path.write_text(f"#!{sys.executable} -S\n" + source, encoding="utf-8")
        path.chmod(0o700)

    def _state_block(self) -> str:
        section = self.runbook.split("### 3.1 ", 1)[1].split("## 4.", 1)[0]
        return re.findall(r"```bash\n(.*?)```", section, re.S)[0]

    def _login_block(self) -> str:
        return self.runbook.split("# 1. login:", 1)[1].split("CSRF=", 1)[0].split("\n", 1)[1]

    def _config(self) -> dict:
        state = ("Result=success\nExecMainCode=1\nExecMainStatus=0\n"
                 "ExecMainStartTimestampMonotonic=200\n"
                 "ExecMainExitTimestampMonotonic=300\nActiveState=inactive\n")
        return {"docker": {}, "before": {"stdout": "100\n"},
                "cursor": {"stdout": json.dumps({"__CURSOR": "synthetic-cursor"})},
                "start": {}, "state": {"stdout": state},
                "state_after": {"stdout": state},
                "journal": {"stdout": _entry(_summary())}}

    def _run_state(self, config: dict) -> subprocess.CompletedProcess:
        (self.root / "config.json").write_text(json.dumps(config))
        (self.root / "show-count").unlink(missing_ok=True)
        return subprocess.run(["bash", "-c", self._state_block()], env=self.env,
                              capture_output=True, text=True, timeout=10)

    def _assert_failed(self, result: subprocess.CompletedProcess) -> None:
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn(SUCCESS, result.stdout)

    def test_current_success_with_zero_orphans(self) -> None:
        result = self._run_state(self._config())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(SUCCESS, result.stdout)

    def test_each_external_command_failure_is_closed(self) -> None:
        for command in self._config():
            with self.subTest(command=command):
                config = self._config()
                config[command]["rc"] = 1
                self._assert_failed(self._run_state(config))

    def test_cursor_and_current_journal_must_be_readable(self) -> None:
        for key in ("cursor", "journal"):
            for output in ("", "not JSON\n", "{}\n"):
                with self.subTest(key=key, output=output):
                    config = self._config()
                    config[key]["stdout"] = output
                    self._assert_failed(self._run_state(config))
        config = self._config()
        config["journal"]["stderr"] = "journal file is truncated"
        self._assert_failed(self._run_state(config))

    def test_current_skip_warning_is_not_success_even_with_summary(self) -> None:
        config = self._config()
        config["journal"]["stdout"] = _entry("WARNING: Skipping orphan cleanup") + _entry(_summary())
        self._assert_failed(self._run_state(config))

    def test_old_warning_does_not_poison_current_success(self) -> None:
        config = self._config()
        config["journal"]["stdout"] = _entry("skipping orphan cleanup", 150) + _entry(_summary())
        result = self._run_state(config)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(SUCCESS, result.stdout)

    def test_old_success_cannot_replace_current_evidence(self) -> None:
        config = self._config()
        config["journal"]["stdout"] = _entry(_summary(), 150) + _entry("Started reaper")
        self._assert_failed(self._run_state(config))

    def test_positive_summary_is_required_and_must_be_unambiguous(self) -> None:
        for output in (_entry("Started reaper"), _entry("{broken"), _entry("{}"),
                       _entry(_summary()) * 2, _entry(_summary(orphan_cleanup=True)),
                       _entry(_summary(orphan_cleanup=-1)),
                       _entry(_summary(auth_session_gc_status="failed"))):
            with self.subTest(output=output):
                config = self._config()
                config["journal"]["stdout"] = output
                self._assert_failed(self._run_state(config))

    def test_service_must_have_a_new_successful_completed_execution(self) -> None:
        for old, new in (("Result=success", "Result=exit-code"),
                         ("ExecMainStatus=0", "ExecMainStatus=1"),
                         ("ExecMainCode=1", "ExecMainCode=2"),
                         ("ActiveState=inactive", "ActiveState=active"),
                         ("Monotonic=200", "Monotonic=100"),
                         ("Monotonic=300", "Monotonic=0"),
                         ("Monotonic=200", "Monotonic="),
                         ("Result=success\n", "")):
            with self.subTest(new=new):
                config = self._config()
                config["state"]["stdout"] = config["state"]["stdout"].replace(old, new)
                self._assert_failed(self._run_state(config))

    def test_concurrent_timer_execution_invalidates_evidence(self) -> None:
        config = self._config()
        config["state_after"]["stdout"] = config["state_after"]["stdout"].replace("Monotonic=200", "Monotonic=400")
        self._assert_failed(self._run_state(config))

    def test_login_serializer_round_trips_special_characters(self) -> None:
        cases = [("user@example.invalid", "abc\"defgh"),
                 ("user@example.invalid", r"abc\ndef\tghi\\j"),
                 ('quote"\\name@example.invalid', "引用符\"・\\・\t・\r\n・🙂"),
                 ("user@example.invalid", "' $HOME $(exit 9) `false` ; & | < >")]
        for email, password in cases:
            with self.subTest(email=email, password=password):
                capture = self.root / "login.json"
                env = {**self.env, "EMAIL": email, "PASSWORD": password,
                       "API": "https://control.invalid", "JAR": str(self.root / "cookies"),
                       "CAPTURE": str(capture)}
                result = subprocess.run(["bash", "-c", self._login_block()], env=env,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                request = json.loads(capture.read_text())
                self.assertEqual(json.loads(request["payload"]), {"email": email, "password": password})
                self.assertNotIn(password, " ".join(request["args"]))

    def test_login_failure_does_not_continue(self) -> None:
        env = {**self.env, "EMAIL": "dummy@example.invalid", "PASSWORD": "dummy",
               "API": "https://control.invalid", "JAR": str(self.root / "cookies"),
               "CAPTURE": str(self.root / "login.json"), "CURL_RC": "22"}
        result = subprocess.run(["bash", "-c", self._login_block() + "\necho CONTINUED"],
                                env=env, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("CONTINUED", result.stdout)


if __name__ == "__main__":
    unittest.main()
