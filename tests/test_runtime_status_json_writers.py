from __future__ import annotations

import ast
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
WRITER_SOURCES = (
    ROOT / "apps" / "continuity" / "continuity.py",
    ROOT / "apps" / "egress-gateway" / "egress.py",
)


def load_runtime_status_writer(source_path: Path):
    """Load only the writer boundary without importing optional GStreamer deps."""

    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    selected = [
        node
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef))
        and node.name in {"RuntimeStatusWriteError", "atomic_write_json"}
    ]
    names = {node.name for node in selected}
    if names != {"RuntimeStatusWriteError", "atomic_write_json"}:
        raise AssertionError(f"runtime status writer contract missing in {source_path}")

    module = ast.Module(body=selected, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "Any": Any,
        "Path": Path,
        "json": json,
        "os": os,
        "tempfile": tempfile,
    }
    exec(compile(module, str(source_path), "exec"), namespace)
    return namespace["atomic_write_json"], namespace["RuntimeStatusWriteError"]


class RuntimeStatusJsonWriterTests(unittest.TestCase):
    def test_valid_payload_remains_compatible(self) -> None:
        payload = {
            "status": "CONNECTED",
            "connected": True,
            "attempt": 1,
            "reason_code": None,
            "updated_at": 123.5,
        }
        for source_path in WRITER_SOURCES:
            with self.subTest(source=source_path):
                writer, _error = load_runtime_status_writer(source_path)
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "status.json"
                    writer(path, payload)
                    self.assertEqual(json.loads(path.read_text(encoding="utf-8")), payload)
                    self.assertEqual(list(path.parent.glob(f".{path.name}.*")), [])

    def test_non_finite_payload_preserves_previous_status_and_cleans_temp(self) -> None:
        previous = '{"status":"CONNECTED","attempt":7}\n'
        for source_path in WRITER_SOURCES:
            writer, error_type = load_runtime_status_writer(source_path)
            for value in (float("nan"), float("inf"), float("-inf")):
                with self.subTest(source=source_path, value=value):
                    with tempfile.TemporaryDirectory() as directory:
                        path = Path(directory) / "status.json"
                        path.write_text(previous, encoding="utf-8")

                        with self.assertRaisesRegex(
                            error_type, "runtime status payload is not strict JSON"
                        ):
                            writer(path, {"status": "CONNECTED", "nested": {"value": value}})

                        self.assertEqual(path.read_text(encoding="utf-8"), previous)
                        self.assertEqual(list(path.parent.glob(f".{path.name}.*")), [])

    def test_unserializable_payload_preserves_previous_status_and_cleans_temp(self) -> None:
        previous = '{"status":"RECONNECTING"}\n'
        for source_path in WRITER_SOURCES:
            with self.subTest(source=source_path):
                writer, error_type = load_runtime_status_writer(source_path)
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "status.json"
                    path.write_text(previous, encoding="utf-8")

                    with self.assertRaisesRegex(
                        error_type, "runtime status payload is not strict JSON"
                    ):
                        writer(path, {"status": "CONNECTED", "value": object()})

                    self.assertEqual(path.read_text(encoding="utf-8"), previous)
                    self.assertEqual(list(path.parent.glob(f".{path.name}.*")), [])


if __name__ == "__main__":
    unittest.main()
