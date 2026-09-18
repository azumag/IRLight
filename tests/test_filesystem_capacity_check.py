from __future__ import annotations

import importlib.util
import io
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-filesystem-capacity.py"

SPEC = importlib.util.spec_from_file_location("irlight_filesystem_capacity_check", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def stats(**overrides: int) -> SimpleNamespace:
    values = {
        "f_bsize": 4096,
        "f_frsize": 4096,
        "f_blocks": 100,
        "f_bfree": 20,
        "f_bavail": 10,
        "f_files": 1000,
        "f_ffree": 500,
        "f_favail": 400,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FilesystemCapacityCheckTests(unittest.TestCase):
    def run_evaluate(self, fake_stats: SimpleNamespace) -> tuple[int, str]:
        output = io.StringIO()
        with mock.patch.object(MODULE.os, "statvfs", return_value=fake_stats):
            with redirect_stdout(output):
                code = MODULE.evaluate_filesystem("/not-echoed")
        return code, output.getvalue()

    def test_available_capacity_is_ok(self) -> None:
        code, output = self.run_evaluate(stats())
        self.assertEqual(code, 0)
        self.assertEqual(
            output,
            "IRLIGHT_FILESYSTEM_CAPACITY status=OK reason=none "
            "available_bytes=40960 available_inodes=400\n",
        )

    def test_block_exhaustion_is_critical(self) -> None:
        code, output = self.run_evaluate(stats(f_bavail=0))
        self.assertEqual(code, 2)
        self.assertIn("status=CRITICAL reason=blocks_exhausted", output)
        self.assertIn("available_bytes=0", output)

    def test_inode_exhaustion_is_critical(self) -> None:
        code, output = self.run_evaluate(stats(f_favail=0))
        self.assertEqual(code, 2)
        self.assertIn("status=CRITICAL reason=inodes_exhausted", output)
        self.assertIn("available_inodes=0", output)

    def test_combined_exhaustion_has_stable_reason(self) -> None:
        code, output = self.run_evaluate(stats(f_bavail=0, f_favail=0))
        self.assertEqual(code, 2)
        self.assertIn("reason=blocks_and_inodes_exhausted", output)

    def test_filesystem_without_inode_accounting_can_still_be_ok(self) -> None:
        code, output = self.run_evaluate(
            stats(f_files=0, f_ffree=0, f_favail=0),
        )
        self.assertEqual(code, 0)
        self.assertIn("available_inodes=unsupported", output)

    def test_zero_fragment_size_uses_block_size(self) -> None:
        code, output = self.run_evaluate(stats(f_frsize=0, f_bsize=1024, f_bavail=3))
        self.assertEqual(code, 0)
        self.assertIn("available_bytes=3072", output)

    def test_unavailable_path_is_unknown_without_path_or_os_error(self) -> None:
        output = io.StringIO()
        with mock.patch.object(
            MODULE.os,
            "statvfs",
            side_effect=OSError("secret-path /operator/private/state"),
        ):
            with redirect_stdout(output):
                code = MODULE.evaluate_filesystem("/operator/private/state")
        self.assertEqual(code, 3)
        self.assertEqual(
            output.getvalue(),
            "IRLIGHT_FILESYSTEM_CAPACITY status=UNKNOWN reason=path_unavailable\n",
        )
        self.assertNotIn("operator", output.getvalue())
        self.assertNotIn("secret-path", output.getvalue())

    def test_invalid_or_unavailable_geometry_is_unknown(self) -> None:
        cases = (
            (stats(f_blocks=0, f_bfree=0, f_bavail=0), "block_capacity_unavailable"),
            (stats(f_bavail=101), "invalid_statvfs"),
            (stats(f_files=10, f_favail=11), "invalid_statvfs"),
            (stats(f_bsize=0, f_frsize=0), "block_capacity_unavailable"),
        )
        for fake_stats, reason in cases:
            with self.subTest(reason=reason):
                code, output = self.run_evaluate(fake_stats)
                self.assertEqual(code, 3)
                self.assertIn(f"reason={reason}", output)

    def test_environment_path_and_argument_precedence(self) -> None:
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"IRLIGHT_FILESYSTEM_PATH": "/from-env"}):
            with mock.patch.object(MODULE.os, "statvfs", return_value=stats()) as statvfs:
                with redirect_stdout(output):
                    code = MODULE.main([])
            self.assertEqual(code, 0)
            statvfs.assert_called_once_with("/from-env")

        output = io.StringIO()
        with mock.patch.dict(os.environ, {"IRLIGHT_FILESYSTEM_PATH": "/from-env"}):
            with mock.patch.object(MODULE.os, "statvfs", return_value=stats()) as statvfs:
                with redirect_stdout(output):
                    code = MODULE.main(["/from-arg"])
            self.assertEqual(code, 0)
            statvfs.assert_called_once_with("/from-arg")

    def test_extra_arguments_fail_closed(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            code = MODULE.main(["/one", "/two"])
        self.assertEqual(code, 3)
        self.assertEqual(
            output.getvalue(),
            "IRLIGHT_FILESYSTEM_CAPACITY status=UNKNOWN reason=invalid_arguments\n",
        )


if __name__ == "__main__":
    unittest.main()
