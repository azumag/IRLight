from __future__ import annotations

import importlib.util
import os
import types
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check-filesystem-readonly.py"
SPEC = importlib.util.spec_from_file_location("filesystem_readonly_check", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FilesystemReadonlyCheckTest(unittest.TestCase):
    def test_writable_filesystem_is_ok(self) -> None:
        fake = types.SimpleNamespace(f_flag=0)
        with mock.patch.object(MODULE.os, "statvfs", return_value=fake):
            code, line = MODULE.evaluate(Path("/state"))
        self.assertEqual(code, 0)
        self.assertEqual(
            line,
            "IRLIGHT_FILESYSTEM_MOUNT_HEALTH status=OK read_only=false",
        )

    def test_read_only_filesystem_is_critical(self) -> None:
        fake = types.SimpleNamespace(f_flag=os.ST_RDONLY)
        with mock.patch.object(MODULE.os, "statvfs", return_value=fake):
            code, line = MODULE.evaluate(Path("/state"))
        self.assertEqual(code, 2)
        self.assertEqual(
            line,
            "IRLIGHT_FILESYSTEM_MOUNT_HEALTH status=CRITICAL reason=filesystem_read_only read_only=true",
        )

    def test_unavailable_path_is_unknown_without_leaking_path(self) -> None:
        target = Path("/state/secret-looking-component")
        with mock.patch.object(MODULE.os, "statvfs", side_effect=OSError("boom")):
            code, line = MODULE.evaluate(target)
        self.assertEqual(code, 3)
        self.assertEqual(
            line,
            "IRLIGHT_FILESYSTEM_MOUNT_HEALTH status=UNKNOWN reason=path_unavailable",
        )
        self.assertNotIn(str(target), line)
        self.assertNotIn("boom", line)

    def test_readonly_flag_unavailable_is_unknown(self) -> None:
        fake = types.SimpleNamespace(f_flag=0)
        with (
            mock.patch.object(MODULE.os, "statvfs", return_value=fake),
            mock.patch.object(MODULE.os, "ST_RDONLY", None),
        ):
            code, line = MODULE.evaluate(Path("/state"))
        self.assertEqual(code, 3)
        self.assertEqual(
            line,
            "IRLIGHT_FILESYSTEM_MOUNT_HEALTH status=UNKNOWN reason=readonly_flag_unavailable",
        )

    def test_argument_precedence_over_environment(self) -> None:
        with mock.patch.dict(
            MODULE.os.environ,
            {"IRLIGHT_FILESYSTEM_PATH": "/env-target", "STATE_DIR": "/state-dir"},
            clear=False,
        ):
            target = MODULE._target_from_args(["check", "/argument-target"])
        self.assertEqual(target, Path("/argument-target"))

    def test_environment_precedence_over_state_dir(self) -> None:
        with mock.patch.dict(
            MODULE.os.environ,
            {"IRLIGHT_FILESYSTEM_PATH": "/env-target", "STATE_DIR": "/state-dir"},
            clear=False,
        ):
            target = MODULE._target_from_args(["check"])
        self.assertEqual(target, Path("/env-target"))

    def test_too_many_arguments_fail_closed(self) -> None:
        with mock.patch("builtins.print") as output:
            code = MODULE.main(["check", "/state", "/other"])
        self.assertEqual(code, 3)
        output.assert_called_once_with(
            "IRLIGHT_FILESYSTEM_MOUNT_HEALTH status=UNKNOWN reason=invalid_target"
        )


if __name__ == "__main__":
    unittest.main()
