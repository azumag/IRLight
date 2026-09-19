from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTROL_API = ROOT / "apps" / "control-api"
sys.path.insert(0, str(CONTROL_API))

from state_mount_identity import (  # noqa: E402
    StateMountIdentityError,
    check_state_mount_identity,
    expected_mount_identity_from_env,
)


class StateMountIdentityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="irlight-readyz-mount-")
        self.root = Path(self.tmp.name)
        self.target = self.root / "state"
        self.target.mkdir()
        self.mountinfo = self.root / "mountinfo"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _entry(
        self,
        *,
        mountpoint: Path | str | None = None,
        root: str = "/",
        source: str = "/dev/state",
        mount_id: int = 36,
    ) -> str:
        target = str(self.target if mountpoint is None else mountpoint)
        return (
            f"{mount_id} 25 0:32 {root} {target} rw,relatime "
            f"- ext4 {source} rw\n"
        )

    def test_no_expectation_preserves_existing_readiness_contract(self) -> None:
        check_state_mount_identity(
            self.root / "does-not-exist",
            expected_source=None,
            expected_root=None,
            mountinfo_path=self.root / "does-not-exist-mountinfo",
        )

    def test_exact_source_and_root_match(self) -> None:
        self.mountinfo.write_text(self._entry(), encoding="utf-8")

        check_state_mount_identity(
            self.target,
            expected_source="/dev/state",
            expected_root="/",
            mountinfo_path=self.mountinfo,
        )

    def test_missing_exact_mountpoint_fails_closed(self) -> None:
        self.mountinfo.write_text(
            self._entry(mountpoint=self.root), encoding="utf-8"
        )

        with self.assertRaisesRegex(
            StateMountIdentityError, "expected state mountpoint is missing"
        ):
            check_state_mount_identity(
                self.target,
                expected_source="/dev/state",
                expected_root=None,
                mountinfo_path=self.mountinfo,
            )

    def test_source_or_root_mismatch_does_not_echo_expected_value(self) -> None:
        secret = "AUDIT_DUMMY_MOUNT_SOURCE"
        self.mountinfo.write_text(self._entry(), encoding="utf-8")

        with self.assertRaises(StateMountIdentityError) as captured:
            check_state_mount_identity(
                self.target,
                expected_source=secret,
                expected_root="/",
                mountinfo_path=self.mountinfo,
            )

        self.assertEqual(str(captured.exception), "state mount identity mismatch")
        self.assertNotIn(secret, str(captured.exception))
        self.assertNotIn(str(self.target), str(captured.exception))

    def test_duplicate_exact_mountpoint_is_ambiguous(self) -> None:
        self.mountinfo.write_text(
            self._entry(mount_id=36) + self._entry(mount_id=37), encoding="utf-8"
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

    def test_symlink_target_is_not_accepted(self) -> None:
        link = self.root / "state-link"
        link.symlink_to(self.target, target_is_directory=True)
        self.mountinfo.write_text(
            self._entry(mountpoint=link), encoding="utf-8"
        )

        with self.assertRaisesRegex(
            StateMountIdentityError, "state mount target is unavailable"
        ):
            check_state_mount_identity(
                link,
                expected_source="/dev/state",
                expected_root=None,
                mountinfo_path=self.mountinfo,
            )

    def test_unrelated_unusual_source_is_not_decoded(self) -> None:
        unrelated = self.root / "other"
        unrelated.mkdir()
        self.mountinfo.write_text(
            self._entry(
                mountpoint=unrelated,
                source=r"broken\999source",
                mount_id=35,
            )
            + self._entry(),
            encoding="utf-8",
        )

        check_state_mount_identity(
            self.target,
            expected_source="/dev/state",
            expected_root="/",
            mountinfo_path=self.mountinfo,
        )

    def test_environment_expectation_is_opt_in_and_validated(self) -> None:
        source_name = "IRLIGHT_TEST_EXPECTED_MOUNT_SOURCE"
        root_name = "IRLIGHT_TEST_EXPECTED_MOUNT_ROOT"
        old_source = os.environ.pop(source_name, None)
        old_root = os.environ.pop(root_name, None)
        try:
            self.assertIsNone(expected_mount_identity_from_env(source_name, root_name))

            os.environ[source_name] = "/dev/state"
            self.assertEqual(
                expected_mount_identity_from_env(source_name, root_name),
                ("/dev/state", None),
            )

            os.environ[root_name] = "relative/root"
            with self.assertRaisesRegex(
                StateMountIdentityError, "invalid expected mount identity"
            ):
                expected_mount_identity_from_env(source_name, root_name)
        finally:
            if old_source is None:
                os.environ.pop(source_name, None)
            else:
                os.environ[source_name] = old_source
            if old_root is None:
                os.environ.pop(root_name, None)
            else:
                os.environ[root_name] = old_root


class ReadyzMountIdentityWiringTest(unittest.TestCase):
    def test_readyz_checks_state_and_node_mount_identity_and_redacts_failure(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-readyz-mount-app-") as temporary:
            root = Path(temporary)
            state_dir = root / "control"
            node_state_dir = root / "node"
            state_dir.mkdir()
            node_state_dir.mkdir()

            env = os.environ.copy()
            env["STATE_DIR"] = str(state_dir)
            env["NODE_STATE_DIR"] = str(node_state_dir)
            env["PYTHONPATH"] = str(CONTROL_API)

            script = textwrap.dedent(
                """
                import os

                import app as control_app
                from fastapi import HTTPException
                from state_mount_identity import StateMountIdentityError

                os.environ["IRLIGHT_READYZ_STATE_EXPECTED_MOUNT_SOURCE"] = "/dev/state"
                os.environ["IRLIGHT_READYZ_STATE_EXPECTED_MOUNT_ROOT"] = "/"
                os.environ["IRLIGHT_READYZ_NODE_STATE_EXPECTED_MOUNT_SOURCE"] = "/dev/node"
                os.environ["IRLIGHT_READYZ_NODE_STATE_EXPECTED_MOUNT_ROOT"] = "/node-root"

                calls = []

                def record(path, *, expected_source, expected_root, mountinfo_path=None):
                    calls.append((str(path), expected_source, expected_root))

                control_app.check_state_mount_identity = record
                assert control_app.readyz() == {"status": "ready"}
                assert calls == [
                    (str(control_app.STATE_DIR), "/dev/state", "/"),
                    (os.environ["NODE_STATE_DIR"], "/dev/node", "/node-root"),
                ]

                sentinel = "AUDIT_DUMMY_MOUNT_IDENTITY_SECRET"

                def fail(path, *, expected_source, expected_root, mountinfo_path=None):
                    raise StateMountIdentityError(f"{sentinel}:{path}")

                control_app.check_state_mount_identity = fail
                try:
                    control_app.readyz()
                except HTTPException as exc:
                    assert exc.status_code == 503
                    assert exc.detail == {"code": "STATE_AUTHORITY_UNAVAILABLE"}
                    rendered = repr(exc.detail)
                    assert sentinel not in rendered
                    assert str(control_app.STATE_DIR) not in rendered
                else:
                    raise AssertionError("mount identity failure unexpectedly reported ready")
                """
            )

            completed = subprocess.run(
                [sys.executable, "-c", script],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(
                completed.returncode,
                0,
                msg=f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
            )


if __name__ == "__main__":
    unittest.main()
