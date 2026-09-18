from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "node-agent"))

import media_stack_inspect_cli as module  # noqa: E402


SERVICES = ("mediamtx", "continuity", "egress-gateway")


def baseline_payload() -> dict[str, object]:
    return {
        "status": "OK",
        "egress_mode": "DIRECT_PUSH",
        "services": [
            {"service": service, "restart_count": index}
            for index, service in enumerate(SERVICES)
        ],
    }


class _ReadHookHandle:
    def __init__(self, handle: object, hook: object) -> None:
        self._handle = handle
        self._hook = hook

    def __enter__(self) -> "_ReadHookHandle":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        self._handle.close()
        return False

    def fileno(self) -> int:
        return self._handle.fileno()

    def read(self, size: int = -1) -> bytes:
        value = self._handle.read(size)
        self._hook()
        return value


class RestartBaselineFileBoundaryTest(unittest.TestCase):
    def _write_baseline(self, root: Path, name: str = "baseline.json") -> Path:
        path = root / name
        path.write_text(json.dumps(baseline_payload()), encoding="utf-8")
        return path

    def test_regular_baseline_still_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_baseline(Path(tmp))
            result = module._load_restart_baseline(
                path, egress_mode="DIRECT_PUSH", services=SERVICES
            )
        self.assertEqual(result, {"mediamtx": 0, "continuity": 1, "egress-gateway": 2})

    def test_final_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            regular = self._write_baseline(root, "regular.json")
            link = root / "baseline.json"
            link.symlink_to(regular.name)
            with self.assertRaisesRegex(
                module.MediaStackInspectError, "restart baseline is unavailable"
            ):
                module._load_restart_baseline(
                    link, egress_mode="DIRECT_PUSH", services=SERVICES
                )

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO requires POSIX mkfifo")
    def test_fifo_is_rejected_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "baseline.fifo"
            os.mkfifo(path)
            with self.assertRaisesRegex(
                module.MediaStackInspectError, "restart baseline is unavailable"
            ):
                module._load_restart_baseline(
                    path, egress_mode="DIRECT_PUSH", services=SERVICES
                )

    def test_oversized_baseline_is_rejected_before_parse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "baseline.json"
            with path.open("wb") as handle:
                handle.truncate(module.MAX_RESTART_BASELINE_BYTES + 1)
            with self.assertRaisesRegex(
                module.MediaStackInspectError, "restart baseline is unavailable"
            ):
                module._load_restart_baseline(
                    path, egress_mode="DIRECT_PUSH", services=SERVICES
                )

    def test_same_inode_mutation_during_read_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = self._write_baseline(root)
            original_open = module._open_restart_baseline_readonly

            def open_with_mutation(target: Path) -> _ReadHookHandle:
                handle = original_open(target)

                def mutate() -> None:
                    with path.open("ab") as writer:
                        writer.write(b" ")
                        writer.flush()
                        os.fsync(writer.fileno())

                return _ReadHookHandle(handle, mutate)

            with patch.object(
                module,
                "_open_restart_baseline_readonly",
                side_effect=open_with_mutation,
            ):
                with self.assertRaisesRegex(
                    module.MediaStackInspectError, "restart baseline is unavailable"
                ):
                    module._load_restart_baseline(
                        path, egress_mode="DIRECT_PUSH", services=SERVICES
                    )

    def test_path_replacement_during_read_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = self._write_baseline(root)
            replacement = self._write_baseline(root, "replacement.json")
            original_open = module._open_restart_baseline_readonly

            def open_with_replacement(target: Path) -> _ReadHookHandle:
                handle = original_open(target)
                return _ReadHookHandle(handle, lambda: os.replace(replacement, path))

            with patch.object(
                module,
                "_open_restart_baseline_readonly",
                side_effect=open_with_replacement,
            ):
                with self.assertRaisesRegex(
                    module.MediaStackInspectError, "restart baseline is unavailable"
                ):
                    module._load_restart_baseline(
                        path, egress_mode="DIRECT_PUSH", services=SERVICES
                    )

    def test_recursive_json_parser_failure_is_controlled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_baseline(Path(tmp))
            with patch.object(
                module.json, "loads", side_effect=RecursionError("too deeply nested")
            ):
                with self.assertRaisesRegex(
                    module.MediaStackInspectError, "restart baseline is unavailable"
                ):
                    module._load_restart_baseline(
                        path, egress_mode="DIRECT_PUSH", services=SERVICES
                    )


if __name__ == "__main__":
    unittest.main()
