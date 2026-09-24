from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-host-pressure.sh"
CGROUP_WRAPPER = ROOT / "scripts" / "check-host-cgroup-memory-events.sh"
CGROUP_CHECKER = ROOT / "scripts" / "check-cgroup-memory-events.sh"


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


class HostCgroupMemoryEventsAggregateTest(unittest.TestCase):
    def _run(
        self,
        *,
        mode: str | None = None,
        current: str | None = None,
        baseline: str | None = None,
        component_code: int = 0,
        configure_current_path: bool = True,
        configure_baseline_path: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="irlight-host-cgroup-memory-events-") as temporary:
            root = Path(temporary)
            scripts = root / "scripts"
            scripts.mkdir(parents=True)
            shutil.copyfile(SCRIPT, scripts / "check-host-pressure.sh")
            shutil.copyfile(CGROUP_WRAPPER, scripts / "check-host-cgroup-memory-events.sh")
            shutil.copyfile(CGROUP_CHECKER, scripts / "check-cgroup-memory-events.sh")

            component = (
                "#!/usr/bin/env bash\n"
                'exit "${IRLIGHT_TEST_COMPONENT_CODE:-0}"\n'
            )
            for name in (
                "check-disk-pressure.sh",
                "check-memory-pressure.sh",
                "check-load-pressure.sh",
                "check-psi-pressure.sh",
                "check-file-handle-pressure.sh",
                "check-conntrack-pressure.sh",
                "check-task-pressure.sh",
            ):
                (scripts / name).write_text(component, encoding="utf-8")

            current_path = root / "memory.events"
            baseline_path = root / "memory.events.baseline"
            current_path.write_text(current if current is not None else events(), encoding="ascii")
            baseline_path.write_text(baseline if baseline is not None else events(), encoding="ascii")

            env = os.environ.copy()
            env["IRLIGHT_TEST_COMPONENT_CODE"] = str(component_code)
            for key in tuple(env):
                if key.startswith("IRLIGHT_HOST_") and key.endswith("_MODE"):
                    env.pop(key)
            if mode is not None:
                env["IRLIGHT_HOST_CGROUP_MEMORY_EVENTS_MODE"] = mode

            if configure_current_path:
                env["IRLIGHT_CGROUP_MEMORY_EVENTS_PATH"] = str(current_path)
            else:
                env.pop("IRLIGHT_CGROUP_MEMORY_EVENTS_PATH", None)
            if configure_baseline_path:
                env["IRLIGHT_CGROUP_MEMORY_EVENTS_BASELINE_PATH"] = str(baseline_path)
            else:
                env.pop("IRLIGHT_CGROUP_MEMORY_EVENTS_BASELINE_PATH", None)

            return subprocess.run(
                [
                    "bash",
                    str(scripts / "check-host-pressure.sh"),
                    "disk",
                    "meminfo",
                    "loadavg",
                    "4",
                    "psi",
                    "file-nr",
                    "conntrack-count",
                    "conntrack-max",
                    "threads-max",
                ],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

    def test_default_output_contract_is_unchanged(self) -> None:
        result = self._run(current=events(oom_kill=1))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_HOST_PRESSURE status=OK disk_status=OK memory_status=OK load_status=OK psi_status=OK file_handle_status=OK conntrack_status=OK task_status=OK",
        )
        self.assertNotIn("cgroup_memory_events_status", result.stdout)

    def test_enabled_without_delta_is_ok(self) -> None:
        result = self._run(mode="enabled")
        self.assertEqual(result.returncode, 0)
        self.assertIn("status=OK", result.stdout)
        self.assertIn("cgroup_memory_events_status=OK", result.stdout)

    def test_pressure_delta_is_warning(self) -> None:
        result = self._run(mode="enabled", current=events(high=1))
        self.assertEqual(result.returncode, 1)
        self.assertIn("status=WARNING", result.stdout)
        self.assertIn("cgroup_memory_events_status=WARNING", result.stdout)

    def test_oom_delta_is_critical(self) -> None:
        result = self._run(mode="enabled", current=events(oom_kill=1))
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("cgroup_memory_events_status=CRITICAL", result.stdout)

    def test_counter_reset_is_unknown(self) -> None:
        result = self._run(
            mode="enabled",
            current=events(high=4),
            baseline=events(high=5),
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("status=UNKNOWN", result.stdout)
        self.assertIn("cgroup_memory_events_status=UNKNOWN", result.stdout)

    def test_malformed_input_is_unknown(self) -> None:
        result = self._run(mode="enabled", current="low 0\nhigh nope\nmax 0\noom 0\noom_kill 0\n")
        self.assertEqual(result.returncode, 3)
        self.assertIn("status=UNKNOWN", result.stdout)
        self.assertIn("cgroup_memory_events_status=UNKNOWN", result.stdout)

    def test_enabled_without_current_target_is_unknown(self) -> None:
        result = self._run(mode="enabled", configure_current_path=False)
        self.assertEqual(result.returncode, 3)
        self.assertIn("status=UNKNOWN", result.stdout)
        self.assertIn("cgroup_memory_events_status=UNKNOWN", result.stdout)

    def test_enabled_without_baseline_target_is_unknown(self) -> None:
        result = self._run(mode="enabled", configure_baseline_path=False)
        self.assertEqual(result.returncode, 3)
        self.assertIn("status=UNKNOWN", result.stdout)
        self.assertIn("cgroup_memory_events_status=UNKNOWN", result.stdout)

    def test_enabled_without_explicit_targets_is_unknown(self) -> None:
        result = self._run(
            mode="enabled",
            configure_current_path=False,
            configure_baseline_path=False,
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("status=UNKNOWN", result.stdout)
        self.assertIn("cgroup_memory_events_status=UNKNOWN", result.stdout)

    def test_other_critical_wins_over_cgroup_unknown(self) -> None:
        result = self._run(
            mode="enabled",
            configure_baseline_path=False,
            component_code=2,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("cgroup_memory_events_status=UNKNOWN", result.stdout)

    def test_invalid_mode_fails_closed_before_components(self) -> None:
        result = self._run(mode="sometimes")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_HOST_PRESSURE status=UNKNOWN reason=invalid_cgroup_memory_events_mode",
        )


if __name__ == "__main__":
    unittest.main()
