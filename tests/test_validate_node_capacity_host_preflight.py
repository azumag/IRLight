from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
VALIDATOR_PATH = ROOT / "scripts" / "validate-node-capacity-host-preflight.py"


def _load(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


VALIDATOR = _load(VALIDATOR_PATH, "node_capacity_host_preflight_evidence_validator_test")


class NodeCapacityHostPreflightEvidenceValidatorTests(unittest.TestCase):
    def _snapshot(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "kind": "irlight-node-capacity-host-preflight",
            "ready": True,
            "platform": {
                "system": "Linux",
                "machine": "x86_64",
                "kernel_release": "6.8.0-test",
            },
            "resources": {
                "logical_cpu_count": 8,
                "memory_total_bytes": 16 * 1024 * 1024 * 1024,
            },
            "docker": {
                "server_version": "28.0.1",
                "compose_version": "2.33.1",
            },
        }

    def test_valid_snapshot_round_trips_from_persisted_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "host-preflight.json"
            snapshot = self._snapshot()
            path.write_text(json.dumps(snapshot) + "\n", encoding="utf-8")
            self.assertEqual(VALIDATOR.validate_snapshot(VALIDATOR.load_snapshot(path)), snapshot)

    def test_unknown_or_secret_bearing_fields_are_rejected(self) -> None:
        snapshots: list[dict[str, object]] = []

        top_level = self._snapshot()
        top_level["hostname"] = "node.example.invalid"
        snapshots.append(top_level)

        nested = self._snapshot()
        nested["docker"] = {
            "server_version": "28.0.1",
            "compose_version": "2.33.1",
            "endpoint": "unix:///var/run/docker.sock",
        }
        snapshots.append(nested)

        for snapshot in snapshots:
            with self.subTest(snapshot=snapshot):
                with self.assertRaises(VALIDATOR.HostPreflightEvidenceError):
                    VALIDATOR.validate_snapshot(snapshot)

    def test_invalid_scalar_types_and_values_are_rejected(self) -> None:
        invalid_snapshots: list[dict[str, object]] = []

        for resources in (
            {"logical_cpu_count": True, "memory_total_bytes": 1024},
            {"logical_cpu_count": 0, "memory_total_bytes": 1024},
            {"logical_cpu_count": 8, "memory_total_bytes": -1},
            {"logical_cpu_count": "8", "memory_total_bytes": 1024},
        ):
            snapshot = self._snapshot()
            snapshot["resources"] = resources
            invalid_snapshots.append(snapshot)

        invalid_docker = self._snapshot()
        invalid_docker["docker"] = {
            "server_version": "28.0.1\nsecret",
            "compose_version": "2.33.1",
        }
        invalid_snapshots.append(invalid_docker)

        invalid_schema = self._snapshot()
        invalid_schema["schema_version"] = True
        invalid_snapshots.append(invalid_schema)

        for snapshot in invalid_snapshots:
            with self.subTest(snapshot=snapshot):
                with self.assertRaises(VALIDATOR.HostPreflightEvidenceError):
                    VALIDATOR.validate_snapshot(snapshot)

    def test_duplicate_keys_and_non_standard_numbers_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            duplicate = pathlib.Path(tmp) / "duplicate.json"
            duplicate.write_text('{"schema_version":1,"schema_version":1}\n', encoding="utf-8")
            with self.assertRaises(VALIDATOR.HostPreflightEvidenceError):
                VALIDATOR.load_snapshot(duplicate)

            non_finite = pathlib.Path(tmp) / "non-finite.json"
            non_finite.write_text('{"schema_version":NaN}\n', encoding="utf-8")
            with self.assertRaises(VALIDATOR.HostPreflightEvidenceError):
                VALIDATOR.load_snapshot(non_finite)

    def test_symlink_and_oversized_evidence_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)
            target = directory / "target.json"
            target.write_text(json.dumps(self._snapshot()), encoding="utf-8")
            link = directory / "link.json"
            link.symlink_to(target)
            with self.assertRaises(VALIDATOR.HostPreflightEvidenceError):
                VALIDATOR.load_snapshot(link)

            oversized = directory / "oversized.json"
            oversized.write_bytes(b"x" * (VALIDATOR.MAX_PREFLIGHT_BYTES + 1))
            with self.assertRaises(VALIDATOR.HostPreflightEvidenceError):
                VALIDATOR.load_snapshot(oversized)

    def test_cli_summary_does_not_echo_host_details(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "host-preflight.json"
            path.write_text(json.dumps(self._snapshot()), encoding="utf-8")
            self.assertEqual(VALIDATOR.main([str(path)]), 0)


if __name__ == "__main__":
    unittest.main()
