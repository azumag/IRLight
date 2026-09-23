from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-node-capacity-host-preflight.py"
SPEC = importlib.util.spec_from_file_location("node_capacity_host_preflight", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class NodeCapacityHostPreflightTest(unittest.TestCase):
    def test_collect_snapshot_reports_only_bounded_non_secret_host_facts(self) -> None:
        commands: list[tuple[str, ...]] = []

        def runner(argv: list[str]) -> str:
            commands.append(tuple(argv))
            if argv[:3] == ["docker", "context", "inspect"]:
                return "unix:///var/run/docker.sock\n"
            if argv[:2] == ["docker", "version"]:
                return "27.5.1\n"
            if argv[:3] == ["docker", "compose", "version"]:
                return "2.33.1\n"
            raise AssertionError(argv)

        snapshot = MODULE.collect_snapshot(
            system_name=lambda: "Linux",
            machine_name=lambda: "x86_64",
            kernel_release=lambda: "6.8.0-57-generic",
            logical_cpu_count=lambda: 8,
            meminfo_reader=lambda: "MemTotal:       16384 kB\nMemFree: 1024 kB\n",
            docker_host_from_environment=lambda: None,
            runner=runner,
        )

        self.assertEqual(snapshot["schema_version"], 1)
        self.assertEqual(snapshot["kind"], "irlight-node-capacity-host-preflight")
        self.assertIs(snapshot["ready"], True)
        self.assertEqual(
            snapshot["platform"],
            {
                "system": "Linux",
                "machine": "x86_64",
                "kernel_release": "6.8.0-57-generic",
            },
        )
        self.assertEqual(
            snapshot["resources"],
            {"logical_cpu_count": 8, "memory_total_bytes": 16384 * 1024},
        )
        self.assertEqual(
            snapshot["docker"],
            {"server_version": "27.5.1", "compose_version": "2.33.1"},
        )
        self.assertEqual(
            commands,
            [
                ("docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"),
                ("docker", "version", "--format", "{{.Server.Version}}"),
                ("docker", "compose", "version", "--short"),
            ],
        )
        self.assertNotIn("hostname", snapshot)
        self.assertNotIn("environment", snapshot)

    def test_non_linux_host_fails_before_local_runtime_probe(self) -> None:
        def unexpected_runner(argv: list[str]) -> str:
            raise AssertionError(f"runner unexpectedly called: {argv}")

        with self.assertRaisesRegex(MODULE.HostPreflightError, "requires Linux"):
            MODULE.collect_snapshot(
                system_name=lambda: "Darwin",
                docker_host_from_environment=lambda: None,
                runner=unexpected_runner,
            )

    def test_invalid_cpu_count_fails_before_docker_probe(self) -> None:
        with self.assertRaisesRegex(MODULE.HostPreflightError, "CPU count"):
            MODULE.collect_snapshot(
                system_name=lambda: "Linux",
                logical_cpu_count=lambda: 0,
                docker_host_from_environment=lambda: None,
                runner=lambda argv: (_ for _ in ()).throw(AssertionError(argv)),
            )

    def test_parse_meminfo_rejects_missing_duplicate_and_invalid_total(self) -> None:
        for value in (
            "MemFree: 1 kB\n",
            "MemTotal: 1 kB\nMemTotal: 1 kB\n",
            "MemTotal: -1 kB\n",
            "MemTotal: 1 MB\n",
            "MemTotal: 0 kB\n",
        ):
            with self.subTest(value=value), self.assertRaises(MODULE.HostPreflightError):
                MODULE.parse_meminfo(value)

    def test_remote_docker_context_is_rejected_before_version_probe(self) -> None:
        commands: list[tuple[str, ...]] = []

        def runner(argv: list[str]) -> str:
            commands.append(tuple(argv))
            if argv[:3] == ["docker", "context", "inspect"]:
                return "ssh://builder@example.invalid"
            raise AssertionError(f"version probe must not run: {argv}")

        with self.assertRaisesRegex(MODULE.HostPreflightError, "non-local Docker endpoint"):
            MODULE.collect_snapshot(
                system_name=lambda: "Linux",
                machine_name=lambda: "x86_64",
                kernel_release=lambda: "6.8.0",
                logical_cpu_count=lambda: 4,
                meminfo_reader=lambda: "MemTotal: 4096 kB\n",
                docker_host_from_environment=lambda: None,
                runner=runner,
            )
        self.assertEqual(len(commands), 1)

    def test_remote_docker_host_environment_is_rejected_without_context_probe(self) -> None:
        with self.assertRaisesRegex(MODULE.HostPreflightError, "non-local Docker endpoint"):
            MODULE.collect_snapshot(
                system_name=lambda: "Linux",
                machine_name=lambda: "x86_64",
                kernel_release=lambda: "6.8.0",
                logical_cpu_count=lambda: 4,
                meminfo_reader=lambda: "MemTotal: 4096 kB\n",
                docker_host_from_environment=lambda: "tcp://10.0.0.2:2376",
                runner=lambda argv: (_ for _ in ()).throw(AssertionError(argv)),
            )

    def test_local_docker_host_environment_skips_context_inspection(self) -> None:
        commands: list[tuple[str, ...]] = []

        def runner(argv: list[str]) -> str:
            commands.append(tuple(argv))
            if argv[:2] == ["docker", "version"]:
                return "27.5.1"
            if argv[:3] == ["docker", "compose", "version"]:
                return "2.33.1"
            raise AssertionError(argv)

        MODULE.collect_snapshot(
            system_name=lambda: "Linux",
            machine_name=lambda: "x86_64",
            kernel_release=lambda: "6.8.0",
            logical_cpu_count=lambda: 4,
            meminfo_reader=lambda: "MemTotal: 4096 kB\n",
            docker_host_from_environment=lambda: "unix:///run/user/1000/docker.sock",
            runner=runner,
        )
        self.assertEqual(commands[0][:2], ("docker", "version"))
        self.assertFalse(any(command[:2] == ("docker", "context") for command in commands))

    def test_unsafe_runtime_version_is_not_emitted(self) -> None:
        def runner(argv: list[str]) -> str:
            if argv[:3] == ["docker", "context", "inspect"]:
                return "unix:///var/run/docker.sock"
            if argv[:2] == ["docker", "version"]:
                return "27.5.1\nINJECTED"
            return "2.33.1"

        with self.assertRaisesRegex(MODULE.HostPreflightError, "unsafe characters"):
            MODULE.collect_snapshot(
                system_name=lambda: "Linux",
                machine_name=lambda: "aarch64",
                kernel_release=lambda: "6.8.0",
                logical_cpu_count=lambda: 4,
                meminfo_reader=lambda: "MemTotal: 4096 kB\n",
                docker_host_from_environment=lambda: None,
                runner=runner,
            )

    def test_epoch_independent_snapshot_has_no_wall_clock_field(self) -> None:
        def runner(argv: list[str]) -> str:
            if argv[:3] == ["docker", "context", "inspect"]:
                return "unix:///var/run/docker.sock"
            return "27.5.1" if argv[:2] == ["docker", "version"] else "2.33.1"

        snapshot = MODULE.collect_snapshot(
            system_name=lambda: "Linux",
            machine_name=lambda: "x86_64",
            kernel_release=lambda: "6.8.0",
            logical_cpu_count=lambda: 2,
            meminfo_reader=lambda: "MemTotal: 2048 kB\n",
            docker_host_from_environment=lambda: None,
            runner=runner,
        )
        self.assertNotIn("observed_at", snapshot)
        self.assertNotIn("timestamp", snapshot)


if __name__ == "__main__":
    unittest.main()
