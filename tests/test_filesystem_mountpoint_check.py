from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check-filesystem-mountpoint.py"
SPEC = importlib.util.spec_from_file_location("filesystem_mountpoint_check", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _record(mountpoint: str) -> str:
    return f"36 29 0:32 / {mountpoint} rw,nosuid,nodev - ext4 /dev/test rw"


class FilesystemMountpointCheckTest(unittest.TestCase):
    def test_exact_mountpoint_is_ok(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-mountpoint-") as temporary:
            root = Path(temporary)
            target = root / "state"
            target.mkdir()
            escaped = str(target).replace("\\", r"\134").replace(" ", r"\040")
            mountinfo = root / "mountinfo"
            mountinfo.write_text(_record(escaped) + "\n", encoding="utf-8")
            code, line = MODULE.evaluate(target, mountinfo)
        self.assertEqual(code, 0)
        self.assertEqual(
            line,
            "IRLIGHT_FILESYSTEM_MOUNTPOINT_HEALTH status=OK mounted=true",
        )

    def test_unrelated_non_utf8_mountpoint_does_not_hide_exact_target(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-mountpoint-") as temporary:
            root = Path(temporary)
            target = root / "state"
            target.mkdir()
            escaped = str(target).replace("\\", r"\134").replace(" ", r"\040")
            mountinfo = root / "mountinfo"
            mountinfo.write_bytes(
                b"35 29 0:31 / /mnt/non-utf8-\xff rw - ext4 /dev/other rw\n"
                + (_record(escaped) + "\n").encode("utf-8")
            )
            code, line = MODULE.evaluate(target, mountinfo)
        self.assertEqual(code, 0)
        self.assertEqual(
            line,
            "IRLIGHT_FILESYSTEM_MOUNTPOINT_HEALTH status=OK mounted=true",
        )

    def test_parent_mount_does_not_make_subdirectory_a_mountpoint(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-mountpoint-") as temporary:
            root = Path(temporary)
            target = root / "state"
            target.mkdir()
            mountinfo = root / "mountinfo"
            mountinfo.write_text(_record(str(root)) + "\n", encoding="utf-8")
            code, line = MODULE.evaluate(target, mountinfo)
        self.assertEqual(code, 2)
        self.assertEqual(
            line,
            "IRLIGHT_FILESYSTEM_MOUNTPOINT_HEALTH status=CRITICAL reason=mountpoint_missing",
        )

    def test_missing_target_is_critical(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-mountpoint-") as temporary:
            root = Path(temporary)
            target = root / "missing"
            mountinfo = root / "mountinfo"
            mountinfo.write_text(_record("/") + "\n", encoding="utf-8")
            code, line = MODULE.evaluate(target, mountinfo)
        self.assertEqual(code, 2)
        self.assertEqual(
            line,
            "IRLIGHT_FILESYSTEM_MOUNTPOINT_HEALTH status=CRITICAL reason=mountpoint_missing",
        )

    def test_mountinfo_escaped_space_is_decoded(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-mountpoint-") as temporary:
            root = Path(temporary)
            target = root / "state volume"
            target.mkdir()
            escaped = str(target).replace(" ", r"\040")
            mountinfo = root / "mountinfo"
            mountinfo.write_text(_record(escaped) + "\n", encoding="utf-8")
            code, line = MODULE.evaluate(target, mountinfo)
        self.assertEqual(code, 0)
        self.assertEqual(
            line,
            "IRLIGHT_FILESYSTEM_MOUNTPOINT_HEALTH status=OK mounted=true",
        )

    def test_unknown_mountinfo_escape_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-mountpoint-") as temporary:
            root = Path(temporary)
            target = root / "state"
            target.mkdir()
            mountinfo = root / "mountinfo"
            mountinfo.write_text(_record(r"/state\777") + "\n", encoding="utf-8")
            code, line = MODULE.evaluate(target, mountinfo)
        self.assertEqual(code, 3)
        self.assertEqual(
            line,
            "IRLIGHT_FILESYSTEM_MOUNTPOINT_HEALTH status=UNKNOWN reason=mountinfo_unavailable",
        )

    def test_malformed_mountinfo_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-mountpoint-") as temporary:
            root = Path(temporary)
            target = root / "state"
            target.mkdir()
            mountinfo = root / "mountinfo"
            mountinfo.write_text("malformed\n", encoding="utf-8")
            code, line = MODULE.evaluate(target, mountinfo)
        self.assertEqual(code, 3)
        self.assertEqual(
            line,
            "IRLIGHT_FILESYSTEM_MOUNTPOINT_HEALTH status=UNKNOWN reason=mountinfo_unavailable",
        )

    def test_missing_mountinfo_is_unknown_without_leaking_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-mountpoint-") as temporary:
            root = Path(temporary)
            target = root / "secret-looking-state"
            target.mkdir()
            mountinfo = root / "secret-looking-mountinfo"
            code, line = MODULE.evaluate(target, mountinfo)
        self.assertEqual(code, 3)
        self.assertEqual(
            line,
            "IRLIGHT_FILESYSTEM_MOUNTPOINT_HEALTH status=UNKNOWN reason=mountinfo_unavailable",
        )
        self.assertNotIn(str(target), line)
        self.assertNotIn(str(mountinfo), line)

    def test_symlink_target_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-mountpoint-") as temporary:
            root = Path(temporary)
            actual = root / "actual"
            actual.mkdir()
            target = root / "state"
            target.symlink_to(actual, target_is_directory=True)
            mountinfo = root / "mountinfo"
            mountinfo.write_text(_record(str(actual)) + "\n", encoding="utf-8")
            code, line = MODULE.evaluate(target, mountinfo)
        self.assertEqual(code, 3)
        self.assertEqual(
            line,
            "IRLIGHT_FILESYSTEM_MOUNTPOINT_HEALTH status=UNKNOWN reason=target_symlink",
        )

    def test_environment_precedence_over_state_dir(self) -> None:
        with mock.patch.dict(
            MODULE.os.environ,
            {
                "IRLIGHT_EXPECTED_MOUNTPOINT_PATH": "/expected",
                "STATE_DIR": "/state",
            },
            clear=False,
        ):
            target = MODULE._target_from_args(["check"])
        self.assertEqual(target, Path("/expected"))

    def test_relative_target_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            MODULE._target_from_args(["check", "relative/state"])

    def test_too_many_arguments_fail_closed(self) -> None:
        with mock.patch("builtins.print") as output:
            code = MODULE.main(["check", "/state", "/other"])
        self.assertEqual(code, 3)
        output.assert_called_once_with(
            "IRLIGHT_FILESYSTEM_MOUNTPOINT_HEALTH status=UNKNOWN reason=invalid_target"
        )


if __name__ == "__main__":
    unittest.main()
