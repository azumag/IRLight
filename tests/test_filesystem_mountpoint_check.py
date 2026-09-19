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


def _escape(value: str) -> str:
    return value.replace("\\", r"\134").replace(" ", r"\040")


def _record(mountpoint: str, *, root: str = "/", source: str = "/dev/test") -> str:
    return f"36 29 0:32 {root} {mountpoint} rw,nosuid,nodev - ext4 {source} rw"


class FilesystemMountpointCheckTest(unittest.TestCase):
    def test_exact_mountpoint_is_ok(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-mountpoint-") as temporary:
            root = Path(temporary)
            target = root / "state"
            target.mkdir()
            escaped = _escape(str(target))
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
            escaped = _escape(str(target))
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

    def test_unrelated_invalid_identity_fields_do_not_change_presence_contract(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-mountpoint-") as temporary:
            root = Path(temporary)
            target = root / "state"
            target.mkdir()
            escaped = _escape(str(target))
            mountinfo = root / "mountinfo"
            mountinfo.write_text(
                _record("/unrelated", root=r"/bad\777", source=r"/dev/bad\777")
                + "\n"
                + _record(escaped)
                + "\n",
                encoding="utf-8",
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
            escaped = _escape(str(target))
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

    def test_expected_source_match_is_ok(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-mountpoint-") as temporary:
            root = Path(temporary)
            target = root / "state"
            target.mkdir()
            mountinfo = root / "mountinfo"
            mountinfo.write_text(
                _record(_escape(str(target)), source="/dev/mapper/irlight-state") + "\n",
                encoding="utf-8",
            )
            code, line = MODULE.evaluate(
                target,
                mountinfo,
                expected_source="/dev/mapper/irlight-state",
            )
        self.assertEqual(code, 0)
        self.assertEqual(
            line,
            "IRLIGHT_FILESYSTEM_MOUNTPOINT_HEALTH status=OK mounted=true",
        )

    def test_expected_source_mismatch_is_critical_without_leaking_identity(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-mountpoint-") as temporary:
            root = Path(temporary)
            target = root / "state"
            target.mkdir()
            mountinfo = root / "mountinfo"
            actual = "/dev/secret-actual-volume"
            expected = "/dev/secret-expected-volume"
            mountinfo.write_text(
                _record(_escape(str(target)), source=actual) + "\n",
                encoding="utf-8",
            )
            code, line = MODULE.evaluate(
                target,
                mountinfo,
                expected_source=expected,
            )
        self.assertEqual(code, 2)
        self.assertEqual(
            line,
            "IRLIGHT_FILESYSTEM_MOUNTPOINT_HEALTH status=CRITICAL reason=mount_identity_mismatch",
        )
        self.assertNotIn(actual, line)
        self.assertNotIn(expected, line)

    def test_expected_root_match_is_ok(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-mountpoint-") as temporary:
            root = Path(temporary)
            target = root / "state"
            target.mkdir()
            mountinfo = root / "mountinfo"
            mountinfo.write_text(
                _record(_escape(str(target)), root="/volumes/state") + "\n",
                encoding="utf-8",
            )
            code, line = MODULE.evaluate(
                target,
                mountinfo,
                expected_root="/volumes/state",
            )
        self.assertEqual(code, 0)
        self.assertEqual(
            line,
            "IRLIGHT_FILESYSTEM_MOUNTPOINT_HEALTH status=OK mounted=true",
        )

    def test_expected_root_mismatch_is_critical(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-mountpoint-") as temporary:
            root = Path(temporary)
            target = root / "state"
            target.mkdir()
            mountinfo = root / "mountinfo"
            mountinfo.write_text(
                _record(_escape(str(target)), root="/volumes/old-state") + "\n",
                encoding="utf-8",
            )
            code, line = MODULE.evaluate(
                target,
                mountinfo,
                expected_root="/volumes/current-state",
            )
        self.assertEqual(code, 2)
        self.assertEqual(
            line,
            "IRLIGHT_FILESYSTEM_MOUNTPOINT_HEALTH status=CRITICAL reason=mount_identity_mismatch",
        )

    def test_escaped_root_and_source_are_decoded_for_identity_match(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-mountpoint-") as temporary:
            root = Path(temporary)
            target = root / "state"
            target.mkdir()
            mountinfo = root / "mountinfo"
            mountinfo.write_text(
                _record(
                    _escape(str(target)),
                    root=r"/volume\040root",
                    source=r"/dev/disk\040name",
                )
                + "\n",
                encoding="utf-8",
            )
            code, line = MODULE.evaluate(
                target,
                mountinfo,
                expected_source="/dev/disk name",
                expected_root="/volume root",
            )
        self.assertEqual(code, 0)
        self.assertEqual(
            line,
            "IRLIGHT_FILESYSTEM_MOUNTPOINT_HEALTH status=OK mounted=true",
        )

    def test_duplicate_exact_mountpoint_is_unknown_when_identity_is_checked(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-mountpoint-") as temporary:
            root = Path(temporary)
            target = root / "state"
            target.mkdir()
            escaped = _escape(str(target))
            mountinfo = root / "mountinfo"
            mountinfo.write_text(
                _record(escaped, source="/dev/first")
                + "\n"
                + _record(escaped, source="/dev/second")
                + "\n",
                encoding="utf-8",
            )
            code, line = MODULE.evaluate(
                target,
                mountinfo,
                expected_source="/dev/second",
            )
        self.assertEqual(code, 3)
        self.assertEqual(
            line,
            "IRLIGHT_FILESYSTEM_MOUNTPOINT_HEALTH status=UNKNOWN reason=mountpoint_ambiguous",
        )

    def test_duplicate_exact_mountpoint_preserves_presence_only_ok_contract(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-mountpoint-") as temporary:
            root = Path(temporary)
            target = root / "state"
            target.mkdir()
            escaped = _escape(str(target))
            mountinfo = root / "mountinfo"
            mountinfo.write_text(
                _record(escaped, source="/dev/first")
                + "\n"
                + _record(escaped, source="/dev/second")
                + "\n",
                encoding="utf-8",
            )
            code, line = MODULE.evaluate(target, mountinfo)
        self.assertEqual(code, 0)
        self.assertEqual(
            line,
            "IRLIGHT_FILESYSTEM_MOUNTPOINT_HEALTH status=OK mounted=true",
        )

    def test_main_applies_identity_environment_to_mountinfo_record(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-mountpoint-") as temporary:
            root = Path(temporary)
            target = root / "state"
            target.mkdir()
            mountinfo = root / "mountinfo"
            mountinfo.write_text(
                _record(
                    _escape(str(target)),
                    root="/volumes/state",
                    source="/dev/mapper/irlight-state",
                )
                + "\n",
                encoding="utf-8",
            )
            with mock.patch.dict(
                MODULE.os.environ,
                {
                    "IRLIGHT_EXPECTED_MOUNTPOINT_PATH": str(target),
                    "IRLIGHT_MOUNTINFO_PATH": str(mountinfo),
                    "IRLIGHT_EXPECTED_MOUNT_SOURCE": "/dev/mapper/irlight-state",
                    "IRLIGHT_EXPECTED_MOUNT_ROOT": "/volumes/state",
                },
                clear=True,
            ), mock.patch("builtins.print") as output:
                code = MODULE.main(["check"])
        self.assertEqual(code, 0)
        output.assert_called_once_with(
            "IRLIGHT_FILESYSTEM_MOUNTPOINT_HEALTH status=OK mounted=true"
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

    def test_identity_environment_is_parsed_without_normalizing_source(self) -> None:
        with mock.patch.dict(
            MODULE.os.environ,
            {
                "IRLIGHT_EXPECTED_MOUNT_SOURCE": "tmpfs",
                "IRLIGHT_EXPECTED_MOUNT_ROOT": "/state/../state",
            },
            clear=True,
        ):
            source, root = MODULE._expected_identity_from_env()
        self.assertEqual(source, "tmpfs")
        self.assertEqual(root, "/state")

    def test_empty_expected_source_fails_closed(self) -> None:
        with mock.patch.dict(
            MODULE.os.environ,
            {"IRLIGHT_EXPECTED_MOUNT_SOURCE": ""},
            clear=True,
        ), mock.patch("builtins.print") as output:
            code = MODULE.main(["check", "/state"])
        self.assertEqual(code, 3)
        output.assert_called_once_with(
            "IRLIGHT_FILESYSTEM_MOUNTPOINT_HEALTH status=UNKNOWN reason=invalid_expected_mount_identity"
        )

    def test_relative_expected_root_fails_closed(self) -> None:
        with mock.patch.dict(
            MODULE.os.environ,
            {"IRLIGHT_EXPECTED_MOUNT_ROOT": "relative/root"},
            clear=True,
        ), mock.patch("builtins.print") as output:
            code = MODULE.main(["check", "/state"])
        self.assertEqual(code, 3)
        output.assert_called_once_with(
            "IRLIGHT_FILESYSTEM_MOUNTPOINT_HEALTH status=UNKNOWN reason=invalid_expected_mount_identity"
        )

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
