from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "record_node_capacity_trial", ROOT / "scripts" / "record-node-capacity-trial.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def cli_args(path: Path, *, sessions: int, outcome: str = "pass") -> list[str]:
    failed_sessions = 0 if outcome == "pass" else 1
    return [
        "--trials-jsonl",
        str(path),
        "--concurrent-sessions",
        str(sessions),
        "--duration-seconds",
        "120",
        "--outcome",
        outcome,
        "--cpu-peak-percent",
        "75.5",
        "--memory-rss-peak-bytes",
        "500000000",
        "--egress-peak-bps",
        "5000000",
        "--failed-sessions",
        str(failed_sessions),
        "--unexpected-reconnects",
        "0",
    ]


def run_main(args: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        result = MODULE.main(args)
    return result, stdout.getvalue(), stderr.getvalue()


class CapacityTrialRecorderTest(unittest.TestCase):
    def test_appends_canonical_monotonic_trials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trials.jsonl"

            result, _, stderr = run_main(cli_args(path, sessions=1))
            self.assertEqual(result, 0, stderr)
            result, _, stderr = run_main(cli_args(path, sessions=4, outcome="fail"))
            self.assertEqual(result, 0, stderr)

            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            first, second = (json.loads(line) for line in lines)
            self.assertEqual(first["concurrent_sessions"], 1)
            self.assertEqual(first["duration_seconds"], 120.0)
            self.assertEqual(first["outcome"], "pass")
            self.assertEqual(second["concurrent_sessions"], 4)
            self.assertEqual(second["outcome"], "fail")
            self.assertEqual(second["failed_sessions"], 1)

    def test_preserves_jsonl_record_boundary_when_existing_file_has_no_final_newline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trials.jsonl"
            result, _, stderr = run_main(cli_args(path, sessions=1))
            self.assertEqual(result, 0, stderr)
            path.write_bytes(path.read_bytes().rstrip(b"\n"))

            result, _, stderr = run_main(cli_args(path, sessions=4, outcome="fail"))
            self.assertEqual(result, 0, stderr)
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0])["concurrent_sessions"], 1)
            self.assertEqual(json.loads(lines[1])["concurrent_sessions"], 4)

    def test_rejects_non_increasing_load_without_modifying_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trials.jsonl"
            result, _, stderr = run_main(cli_args(path, sessions=2))
            self.assertEqual(result, 0, stderr)
            before = path.read_bytes()

            result, _, stderr = run_main(cli_args(path, sessions=2))
            self.assertEqual(result, 2)
            self.assertIn("strictly increasing", stderr)
            self.assertEqual(path.read_bytes(), before)

    def test_rejects_pass_after_failed_load_without_modifying_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trials.jsonl"
            self.assertEqual(run_main(cli_args(path, sessions=1))[0], 0)
            self.assertEqual(run_main(cli_args(path, sessions=4, outcome="fail"))[0], 0)
            before = path.read_bytes()

            result, _, stderr = run_main(cli_args(path, sessions=8))
            self.assertEqual(result, 2)
            self.assertIn("passing trial cannot appear above", stderr)
            self.assertEqual(path.read_bytes(), before)

    def test_rejects_invalid_trial_before_creating_evidence_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trials.jsonl"
            args = cli_args(path, sessions=1)
            args[args.index("--cpu-peak-percent") + 1] = "nan"

            result, _, stderr = run_main(args)
            self.assertEqual(result, 2)
            self.assertIn("finite number", stderr)
            self.assertFalse(path.exists())
            self.assertFalse(path.with_name(f"{path.name}.lock").exists())

    def test_rejects_pass_that_reports_failed_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trials.jsonl"
            args = cli_args(path, sessions=1)
            args[args.index("--failed-sessions") + 1] = "1"

            result, _, stderr = run_main(args)
            self.assertEqual(result, 2)
            self.assertIn("cannot claim pass", stderr)
            self.assertFalse(path.exists())
            self.assertFalse(path.with_name(f"{path.name}.lock").exists())

    def test_rejects_corrupt_existing_jsonl_without_modifying_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trials.jsonl"
            path.write_text('{"outcome":"pass"\n', encoding="utf-8")
            before = path.read_bytes()

            result, _, stderr = run_main(cli_args(path, sessions=2))
            self.assertEqual(result, 2)
            self.assertIn("invalid JSON", stderr)
            self.assertEqual(path.read_bytes(), before)

    def test_refuses_symbolic_link_evidence_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            target = directory / "target.jsonl"
            target.write_text("sentinel\n", encoding="utf-8")
            link = directory / "trials.jsonl"
            try:
                link.symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest("symbolic links are unavailable")

            result, _, stderr = run_main(cli_args(link, sessions=1))
            self.assertEqual(result, 2)
            self.assertIn("symbolic link", stderr)
            self.assertEqual(target.read_text(encoding="utf-8"), "sentinel\n")


if __name__ == "__main__":
    unittest.main()
