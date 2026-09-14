from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-cgroup-memory-events.sh"


def events(**overrides: int) -> str:
    values = {
        "low": 0,
        "high": 0,
        "max": 0,
        "oom": 0,
        "oom_kill": 0,
        "oom_group_kill": 0,
    }
    values.update(overrides)
    return "".join(f"{key} {value}\n" for key, value in values.items())


class CgroupMemoryEventsCheckTest(unittest.TestCase):
    def _run(
        self,
        current: str | None = None,
        baseline: str | None = None,
        *,
        env_overrides: dict[str, str] | None = None,
        create_current: bool = True,
        create_baseline: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="irlight-cgroup-memory-events-") as temporary:
            root = Path(temporary)
            current_path = root / "memory.events.current"
            baseline_path = root / "memory.events.baseline"
            if create_current:
                current_path.write_text(current or events(), encoding="utf-8")
            if create_baseline:
                baseline_path.write_text(baseline or events(), encoding="utf-8")
            env = os.environ.copy()
            if env_overrides:
                env.update(env_overrides)
            return subprocess.run(
                ["bash", str(SCRIPT), str(current_path), str(baseline_path)],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

    def test_no_new_events_is_ok(self) -> None:
        baseline = events(low=2, high=3, max=4, oom=5, oom_kill=6, oom_group_kill=7)
        result = self._run(current=baseline, baseline=baseline)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_CGROUP_MEMORY_EVENTS status=OK reason=none low_delta=0 high_delta=0 max_delta=0 oom_delta=0 oom_kill_delta=0 oom_group_kill_delta=0",
        )

    def test_new_high_event_is_warning(self) -> None:
        result = self._run(current=events(high=1))
        self.assertEqual(result.returncode, 1)
        self.assertIn("status=WARNING", result.stdout)
        self.assertIn("reason=memory_pressure_activity", result.stdout)
        self.assertIn("high_delta=1", result.stdout)

    def test_new_max_event_is_warning(self) -> None:
        result = self._run(current=events(max=2))
        self.assertEqual(result.returncode, 1)
        self.assertIn("max_delta=2", result.stdout)

    def test_new_low_event_is_warning(self) -> None:
        result = self._run(current=events(low=1))
        self.assertEqual(result.returncode, 1)
        self.assertIn("low_delta=1", result.stdout)

    def test_new_oom_event_is_critical(self) -> None:
        result = self._run(current=events(oom=1))
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("reason=oom_activity", result.stdout)
        self.assertIn("oom_delta=1", result.stdout)

    def test_new_oom_kill_is_critical(self) -> None:
        result = self._run(current=events(oom_kill=1))
        self.assertEqual(result.returncode, 2)
        self.assertIn("oom_kill_delta=1", result.stdout)

    def test_optional_oom_group_kill_is_supported(self) -> None:
        baseline = "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n"
        current = baseline + "oom_group_kill 1\n"
        result = self._run(current=current, baseline=baseline)
        self.assertEqual(result.returncode, 2)
        self.assertIn("oom_group_kill_delta=1", result.stdout)

    def test_unknown_future_counter_is_ignored_after_validation(self) -> None:
        baseline = events() + "future_counter 10\n"
        current = events(high=1) + "future_counter 11\n"
        result = self._run(current=current, baseline=baseline)
        self.assertEqual(result.returncode, 1)
        self.assertIn("high_delta=1", result.stdout)

    def test_counter_reset_is_unknown(self) -> None:
        result = self._run(current=events(high=4), baseline=events(high=5))
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_CGROUP_MEMORY_EVENTS status=UNKNOWN reason=counter_reset",
        )

    def test_missing_required_counter_is_unknown(self) -> None:
        result = self._run(current="low 0\nhigh 0\nmax 0\noom 0\n")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_CGROUP_MEMORY_EVENTS status=UNKNOWN reason=missing_required_counter",
        )

    def test_duplicate_counter_is_unknown(self) -> None:
        result = self._run(current=events() + "high 0\n")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_CGROUP_MEMORY_EVENTS status=UNKNOWN reason=duplicate_events_record",
        )

    def test_malformed_record_is_unknown(self) -> None:
        for content in (
            events() + "broken\n",
            events() + "future_counter nope\n",
            events() + "high 1 extra\n",
            events() + "future_counter 9223372036854775808\n",
        ):
            with self.subTest(content=content):
                result = self._run(current=content)
                self.assertEqual(result.returncode, 3)
                self.assertEqual(
                    result.stdout.strip(),
                    "IRLIGHT_CGROUP_MEMORY_EVENTS status=UNKNOWN reason=invalid_events_record",
                )

    def test_missing_current_or_baseline_is_unknown(self) -> None:
        current_missing = self._run(create_current=False)
        self.assertEqual(current_missing.returncode, 3)
        self.assertEqual(
            current_missing.stdout.strip(),
            "IRLIGHT_CGROUP_MEMORY_EVENTS status=UNKNOWN reason=current_events_unavailable",
        )
        baseline_missing = self._run(create_baseline=False)
        self.assertEqual(baseline_missing.returncode, 3)
        self.assertEqual(
            baseline_missing.stdout.strip(),
            "IRLIGHT_CGROUP_MEMORY_EVENTS status=UNKNOWN reason=baseline_events_unavailable",
        )

    def test_signed_64_bit_maximum_is_supported(self) -> None:
        baseline = events(high=9223372036854775806)
        current = events(high=9223372036854775807)
        result = self._run(current=current, baseline=baseline)
        self.assertEqual(result.returncode, 1)
        self.assertIn("high_delta=1", result.stdout)


if __name__ == "__main__":
    unittest.main()
