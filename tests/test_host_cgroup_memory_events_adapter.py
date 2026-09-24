from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "scripts" / "check-host-cgroup-memory-events.sh"
CHECKER = ROOT / "scripts" / "check-cgroup-memory-events.sh"


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


class HostCgroupMemoryEventsAdapterTest(unittest.TestCase):
    def _run(
        self,
        *,
        current: str = events(),
        baseline: str = events(),
        configure_current: bool = True,
        configure_baseline: bool = True,
        include_checker: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="irlight-host-cgroup-memory-events-") as temporary:
            root = Path(temporary)
            scripts = root / "scripts"
            scripts.mkdir()
            shutil.copyfile(WRAPPER, scripts / WRAPPER.name)
            if include_checker:
                shutil.copyfile(CHECKER, scripts / CHECKER.name)

            current_path = root / "memory.events"
            baseline_path = root / "memory.events.baseline"
            current_path.write_text(current, encoding="ascii")
            baseline_path.write_text(baseline, encoding="ascii")

            command = ["bash", str(scripts / WRAPPER.name)]
            if configure_current:
                command.append(str(current_path))
                if configure_baseline:
                    command.append(str(baseline_path))
            elif configure_baseline:
                command.extend(["", str(baseline_path)])

            return subprocess.run(
                command,
                text=True,
                capture_output=True,
                check=False,
            )

    def test_explicit_same_generation_snapshot_without_delta_is_ok(self) -> None:
        result = self._run()
        self.assertEqual(result.returncode, 0)
        self.assertIn("status=OK", result.stdout)
        self.assertIn("reason=none", result.stdout)

    def test_pressure_delta_is_warning(self) -> None:
        result = self._run(current=events(high=1))
        self.assertEqual(result.returncode, 1)
        self.assertIn("status=WARNING", result.stdout)
        self.assertIn("reason=memory_pressure_activity", result.stdout)

    def test_oom_delta_is_critical(self) -> None:
        result = self._run(current=events(oom_kill=1))
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("reason=oom_activity", result.stdout)

    def test_counter_reset_is_unknown(self) -> None:
        result = self._run(current=events(high=0), baseline=events(high=1))
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_CGROUP_MEMORY_EVENTS status=UNKNOWN reason=counter_reset",
        )

    def test_missing_current_target_fails_closed(self) -> None:
        result = self._run(configure_current=False)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_CGROUP_MEMORY_EVENTS status=UNKNOWN reason=target_not_configured",
        )

    def test_missing_baseline_target_fails_closed(self) -> None:
        result = self._run(configure_baseline=False)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_CGROUP_MEMORY_EVENTS status=UNKNOWN reason=target_not_configured",
        )

    def test_missing_checker_fails_closed(self) -> None:
        result = self._run(include_checker=False)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_CGROUP_MEMORY_EVENTS status=UNKNOWN reason=checker_unavailable",
        )


if __name__ == "__main__":
    unittest.main()
