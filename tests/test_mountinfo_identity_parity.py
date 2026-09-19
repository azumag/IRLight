from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-filesystem-mountpoint.py"
CONTROL_API = ROOT / "apps" / "control-api"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(CONTROL_API) not in sys.path:
    sys.path.insert(0, str(CONTROL_API))

SPEC = importlib.util.spec_from_file_location("filesystem_mountpoint_parity", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
HOST_CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HOST_CHECK)

from state_mount_identity import (  # noqa: E402
    StateMountIdentityError,
    check_state_mount_identity,
)


def _escape(value: str) -> str:
    return value.replace("\\", r"\134").replace(" ", r"\040")


def _record(
    mountpoint: str,
    *,
    root: str = "/",
    source: str = "/dev/state",
    mount_id: int = 36,
) -> str:
    return (
        f"{mount_id} 25 0:32 {root} {mountpoint} rw,relatime "
        f"- ext4 {source} rw"
    )


class MountinfoIdentityParityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="irlight-mount-parity-")
        self.root = Path(self.tmp.name)
        self.target = self.root / "state"
        self.target.mkdir()
        self.mountinfo = self.root / "mountinfo"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _assert_both_ok(
        self,
        *,
        expected_source: str | None,
        expected_root: str | None,
    ) -> None:
        code, line = HOST_CHECK.evaluate(
            self.target,
            self.mountinfo,
            expected_source=expected_source,
            expected_root=expected_root,
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            line,
            "IRLIGHT_FILESYSTEM_MOUNTPOINT_HEALTH status=OK mounted=true",
        )
        check_state_mount_identity(
            self.target,
            expected_source=expected_source,
            expected_root=expected_root,
            mountinfo_path=self.mountinfo,
        )

    def test_ambiguous_duplicate_is_fail_closed_for_both_consumers(self) -> None:
        escaped = _escape(str(self.target))
        self.mountinfo.write_text(
            _record(escaped, source="/dev/state", mount_id=36)
            + "\n"
            + _record(escaped, source="/dev/state", mount_id=37)
            + "\n",
            encoding="utf-8",
        )

        code, line = HOST_CHECK.evaluate(
            self.target,
            self.mountinfo,
            expected_source="/dev/state",
        )
        self.assertEqual(code, 3)
        self.assertEqual(
            line,
            "IRLIGHT_FILESYSTEM_MOUNTPOINT_HEALTH status=UNKNOWN reason=mountpoint_ambiguous",
        )
        with self.assertRaisesRegex(
            StateMountIdentityError, "state mountpoint identity is ambiguous"
        ):
            check_state_mount_identity(
                self.target,
                expected_source="/dev/state",
                expected_root=None,
                mountinfo_path=self.mountinfo,
            )

    def test_escaped_space_and_backslash_match_for_both_consumers(self) -> None:
        escaped_target = self.root / "state volume\\blue"
        self.target.rmdir()
        escaped_target.mkdir()
        self.target = escaped_target
        expected_source = "/dev/disk name\\blue"
        expected_root = "/volume root\\blue"
        self.mountinfo.write_text(
            _record(
                _escape(str(self.target)),
                root=_escape(expected_root),
                source=_escape(expected_source),
            )
            + "\n",
            encoding="utf-8",
        )

        self._assert_both_ok(
            expected_source=expected_source,
            expected_root=expected_root,
        )

    def test_unrelated_non_utf8_record_does_not_poison_either_consumer(self) -> None:
        target_record = (
            _record(_escape(str(self.target)), root="/", source="/dev/state") + "\n"
        ).encode("utf-8")
        self.mountinfo.write_bytes(
            b"35 25 0:31 / /mnt/non-utf8-\xff rw - ext4 /dev/other rw\n"
            + target_record
        )

        self._assert_both_ok(expected_source="/dev/state", expected_root="/")

    def test_source_or_root_mismatch_is_critical_for_both_consumers(self) -> None:
        self.mountinfo.write_text(
            _record(
                _escape(str(self.target)),
                root="/volume/old",
                source="/dev/old-state",
            )
            + "\n",
            encoding="utf-8",
        )

        code, line = HOST_CHECK.evaluate(
            self.target,
            self.mountinfo,
            expected_source="/dev/current-state",
            expected_root="/volume/current",
        )
        self.assertEqual(code, 2)
        self.assertEqual(
            line,
            "IRLIGHT_FILESYSTEM_MOUNTPOINT_HEALTH status=CRITICAL reason=mount_identity_mismatch",
        )
        with self.assertRaisesRegex(StateMountIdentityError, "state mount identity mismatch"):
            check_state_mount_identity(
                self.target,
                expected_source="/dev/current-state",
                expected_root="/volume/current",
                mountinfo_path=self.mountinfo,
            )

    def test_presence_only_host_check_keeps_unrelated_identity_fields_opaque(self) -> None:
        self.mountinfo.write_text(
            _record("/unrelated", root=r"/bad\777", source=r"/dev/bad\777", mount_id=35)
            + "\n"
            + _record(_escape(str(self.target)))
            + "\n",
            encoding="utf-8",
        )

        code, line = HOST_CHECK.evaluate(self.target, self.mountinfo)
        self.assertEqual(code, 0)
        self.assertEqual(
            line,
            "IRLIGHT_FILESYSTEM_MOUNTPOINT_HEALTH status=OK mounted=true",
        )

    def test_host_script_resolves_shared_parser_from_non_repo_cwd(self) -> None:
        self.mountinfo.write_text(
            _record(_escape(str(self.target)), source="/dev/state") + "\n",
            encoding="utf-8",
        )
        env = os.environ.copy()
        env.update(
            {
                "IRLIGHT_MOUNTINFO_PATH": str(self.mountinfo),
                "IRLIGHT_EXPECTED_MOUNT_SOURCE": "/dev/state",
            }
        )
        with tempfile.TemporaryDirectory(prefix="irlight-external-cwd-") as cwd:
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), str(self.target)],
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        self.assertEqual(
            completed.stdout.strip(),
            "IRLIGHT_FILESYSTEM_MOUNTPOINT_HEALTH status=OK mounted=true",
        )


if __name__ == "__main__":
    unittest.main()
