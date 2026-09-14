from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-cgroup-runtime-pressure.sh"


class CgroupRuntimePressureTest(unittest.TestCase):
    def _run(
        self,
        *,
        memory_current: str = "100",
        memory_max: str = "1000",
        memory_high: str = "800",
        pids_current: str = "10",
        pids_max: str = "100",
        current_events: dict[str, int] | None = None,
        baseline_events: dict[str, int] | None = None,
        process_fd_count: int | None = None,
        process_limits: str | None = None,
        swap_current: str = "0",
        swap_max: str = "max",
        include_swap: bool = False,
        env_overrides: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="irlight-cgroup-runtime-pressure-") as temporary:
            root = Path(temporary)
            cgroup_dir = root / "cgroup"
            cgroup_dir.mkdir()

            (cgroup_dir / "memory.current").write_text(f"{memory_current}\n", encoding="utf-8")
            (cgroup_dir / "memory.max").write_text(f"{memory_max}\n", encoding="utf-8")
            (cgroup_dir / "memory.high").write_text(f"{memory_high}\n", encoding="utf-8")
            (cgroup_dir / "pids.current").write_text(f"{pids_current}\n", encoding="utf-8")
            (cgroup_dir / "pids.max").write_text(f"{pids_max}\n", encoding="utf-8")
            (cgroup_dir / "cpu.pressure").write_text(
                "some avg10=1.00 avg60=0.50 avg300=0.25 total=12345\n",
                encoding="utf-8",
            )
            for resource in ("memory", "io"):
                (cgroup_dir / f"{resource}.pressure").write_text(
                    "some avg10=1.00 avg60=0.50 avg300=0.25 total=12345\n"
                    "full avg10=0.00 avg60=0.10 avg300=0.05 total=1234\n",
                    encoding="utf-8",
                )

            if include_swap:
                (cgroup_dir / "memory.swap.current").write_text(
                    f"{swap_current}\n", encoding="utf-8"
                )
                (cgroup_dir / "memory.swap.max").write_text(
                    f"{swap_max}\n", encoding="utf-8"
                )

            baseline_path: Path | None = None
            if current_events is not None or baseline_events is not None:
                defaults = {
                    "low": 0,
                    "high": 0,
                    "max": 0,
                    "oom": 0,
                    "oom_kill": 0,
                    "oom_group_kill": 0,
                }
                current = defaults | (current_events or {})
                baseline = defaults | (baseline_events or {})
                (cgroup_dir / "memory.events").write_text(
                    "".join(f"{key} {value}\n" for key, value in current.items()),
                    encoding="utf-8",
                )
                baseline_path = root / "memory.events.baseline"
                baseline_path.write_text(
                    "".join(f"{key} {value}\n" for key, value in baseline.items()),
                    encoding="utf-8",
                )

            process_dir: Path | None = None
            if process_fd_count is not None or process_limits is not None:
                process_dir = root / "process"
                fd_dir = process_dir / "fd"
                fd_dir.mkdir(parents=True)
                for fd_number in range(process_fd_count or 0):
                    (fd_dir / str(fd_number)).write_text("", encoding="utf-8")
                (process_dir / "limits").write_text(
                    process_limits
                    if process_limits is not None
                    else "Max open files            100                  200                  files\n",
                    encoding="utf-8",
                )

            env = os.environ.copy()
            if env_overrides:
                env.update(env_overrides)

            command = ["bash", str(SCRIPT), str(cgroup_dir)]
            if baseline_path is not None or process_dir is not None or include_swap:
                command.append(str(baseline_path) if baseline_path is not None else "")
            if process_dir is not None or include_swap:
                command.append(str(process_dir) if process_dir is not None else "")
            if include_swap:
                command.append(str(cgroup_dir))
            return subprocess.run(
                command,
                env=env,
                text=True,
                capture_output=True,
                check=False,
                timeout=8,
            )

    def test_target_is_required(self) -> None:
        result = subprocess.run(
            ["bash", str(SCRIPT)],
            env=os.environ.copy(),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_CGROUP_RUNTIME_PRESSURE status=UNKNOWN reason=target_required",
        )

    def test_all_stateless_components_ok_without_optional_targets(self) -> None:
        result = self._run()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_CGROUP_RUNTIME_PRESSURE status=OK memory_max_status=OK memory_high_status=OK pids_status=OK psi_status=OK memory_events_status=NOT_CONFIGURED process_fd_status=NOT_CONFIGURED swap_status=NOT_CONFIGURED",
        )

    def test_warning_component_sets_warning(self) -> None:
        result = self._run(memory_current="650")
        self.assertEqual(result.returncode, 1)
        self.assertIn("status=WARNING", result.stdout)
        self.assertIn("memory_max_status=OK", result.stdout)
        self.assertIn("memory_high_status=WARNING", result.stdout)

    def test_known_critical_wins_over_unrelated_unknown(self) -> None:
        result = self._run(memory_max="invalid", pids_current="100")
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("memory_max_status=UNKNOWN", result.stdout)
        self.assertIn("pids_status=CRITICAL", result.stdout)

    def test_memory_events_oom_delta_is_included_when_baseline_is_explicit(self) -> None:
        result = self._run(
            current_events={"oom": 1, "oom_kill": 1},
            baseline_events={"oom": 0, "oom_kill": 0},
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("memory_events_status=CRITICAL", result.stdout)

    def test_memory_events_counter_reset_fails_closed(self) -> None:
        result = self._run(
            current_events={"high": 1},
            baseline_events={"high": 2},
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("status=UNKNOWN", result.stdout)
        self.assertIn("memory_events_status=UNKNOWN", result.stdout)

    def test_process_fd_warning_is_included_when_process_is_explicit(self) -> None:
        result = self._run(process_fd_count=85)
        self.assertEqual(result.returncode, 1)
        self.assertIn("status=WARNING", result.stdout)
        self.assertIn("process_fd_status=WARNING", result.stdout)
        self.assertIn("memory_events_status=NOT_CONFIGURED", result.stdout)

    def test_process_fd_critical_contributes_to_aggregate_precedence(self) -> None:
        result = self._run(process_fd_count=95, memory_max="invalid")
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("memory_max_status=UNKNOWN", result.stdout)
        self.assertIn("process_fd_status=CRITICAL", result.stdout)

    def test_invalid_process_limits_fail_closed(self) -> None:
        result = self._run(process_fd_count=10, process_limits="invalid\n")
        self.assertEqual(result.returncode, 3)
        self.assertIn("status=UNKNOWN", result.stdout)
        self.assertIn("process_fd_status=UNKNOWN", result.stdout)

    def test_swap_warning_is_included_only_when_explicit(self) -> None:
        result = self._run(include_swap=True, swap_current="85", swap_max="100")
        self.assertEqual(result.returncode, 1)
        self.assertIn("status=WARNING", result.stdout)
        self.assertIn("swap_status=WARNING", result.stdout)

    def test_swap_critical_contributes_to_aggregate_precedence(self) -> None:
        result = self._run(
            include_swap=True,
            swap_current="95",
            swap_max="100",
            memory_max="invalid",
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=CRITICAL", result.stdout)
        self.assertIn("memory_max_status=UNKNOWN", result.stdout)
        self.assertIn("swap_status=CRITICAL", result.stdout)

    def test_missing_swap_control_file_fails_closed_when_explicit(self) -> None:
        result = self._run(include_swap=True, swap_current="10", swap_max="invalid")
        self.assertEqual(result.returncode, 3)
        self.assertIn("status=UNKNOWN", result.stdout)
        self.assertIn("swap_status=UNKNOWN", result.stdout)

    def test_invalid_timeout_fails_closed_without_unbounded_fallback(self) -> None:
        result = self._run(
            env_overrides={"IRLIGHT_CGROUP_COMPONENT_TIMEOUT_SECONDS": "0"}
        )
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip(),
            "IRLIGHT_CGROUP_RUNTIME_PRESSURE status=UNKNOWN memory_max_status=UNKNOWN memory_high_status=UNKNOWN pids_status=UNKNOWN psi_status=UNKNOWN memory_events_status=NOT_CONFIGURED process_fd_status=NOT_CONFIGURED swap_status=NOT_CONFIGURED",
        )


if __name__ == "__main__":
    unittest.main()
