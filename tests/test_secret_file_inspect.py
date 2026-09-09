from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "node-agent"))

from secret_file_inspect_cli import (  # noqa: E402
    inspect_secret_path,
    inspect_secret_paths,
    main,
)


class SecretFileInspectTest(unittest.TestCase):
    def _secret(self, root: Path, *, file_mode: int = 0o600) -> Path:
        secret_dir = root / "secrets"
        secret_dir.mkdir(mode=0o700)
        secret_dir.chmod(0o700)
        path = secret_dir / "token"
        path.write_text("do-not-read\n", encoding="utf-8")
        path.chmod(file_mode)
        return path

    def test_safe_regular_secret_is_ok_without_reading_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._secret(Path(tmp))
            with mock.patch.object(
                Path,
                "read_text",
                side_effect=AssertionError("secret content must not be read"),
            ):
                result = inspect_secret_path(path)

        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["file_mode"], "0600")
        self.assertEqual(result["parent_mode"], "0700")
        self.assertNotIn("do-not-read", json.dumps(result))

    def test_group_or_world_permissions_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = self._secret(root, file_mode=0o640)
            path.parent.chmod(0o750)
            result = inspect_secret_path(path)

        self.assertEqual(result["status"], "PROBLEM")
        self.assertIn("permissions_too_open", result["problems"])
        self.assertIn("parent_permissions_too_open", result["problems"])

    def test_symlink_is_rejected_without_following_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = self._secret(root)
            link = target.parent / "token-link"
            os.symlink(target, link)
            result = inspect_secret_path(link)

        self.assertEqual(result["status"], "PROBLEM")
        self.assertIn("symlink", result["problems"])
        self.assertNotIn("file_mode", result)

    def test_parent_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = self._secret(root)
            parent_link = root / "secret-link"
            os.symlink(target.parent, parent_link)
            result = inspect_secret_path(parent_link / target.name)

        self.assertEqual(result["status"], "PROBLEM")
        self.assertIn("parent_symlink", result["problems"])
        self.assertNotIn("file_mode", result)

    def test_parent_identity_change_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._secret(Path(tmp))
            real_fstat = os.fstat

            def changed_identity(fd: int) -> os.stat_result:
                current = real_fstat(fd)
                fields = list(current)
                fields[1] = current.st_ino + 1
                return os.stat_result(fields)

            with mock.patch(
                "secret_file_inspect_cli.os.fstat", side_effect=changed_identity
            ):
                result = inspect_secret_path(path)

        self.assertEqual(result["status"], "PROBLEM")
        self.assertIn("parent_changed", result["problems"])
        self.assertNotIn("file_mode", result)

    def test_file_identity_change_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._secret(Path(tmp))
            real_stat = os.stat
            file_stat_calls = 0

            def changed_identity(*args: object, **kwargs: object) -> os.stat_result:
                nonlocal file_stat_calls
                current = real_stat(*args, **kwargs)
                if args and args[0] == path.name and kwargs.get("dir_fd") is not None:
                    file_stat_calls += 1
                    if file_stat_calls == 2:
                        fields = list(current)
                        fields[1] = current.st_ino + 1
                        return os.stat_result(fields)
                return current

            with mock.patch(
                "secret_file_inspect_cli.os.stat", side_effect=changed_identity
            ):
                result = inspect_secret_path(path)

        self.assertEqual(result["status"], "PROBLEM")
        self.assertIn("file_changed", result["problems"])

    def test_missing_secret_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret_dir = root / "secrets"
            secret_dir.mkdir(mode=0o700)
            secret_dir.chmod(0o700)
            result = inspect_secret_path(secret_dir / "missing")

        self.assertEqual(result["status"], "PROBLEM")
        self.assertIn("missing", result["problems"])

    def test_cli_returns_nonzero_for_unsafe_secret_and_emits_metadata_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._secret(Path(tmp), file_mode=0o644)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exit_code = main(["--path", str(path)])

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "PROBLEM")
        self.assertEqual(payload["problem_count"], 1)
        self.assertNotIn("do-not-read", output.getvalue())

    def test_multiple_safe_paths_are_aggregated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_dir = root / "one"
            second_dir = root / "two"
            first_dir.mkdir(mode=0o700)
            second_dir.mkdir(mode=0o700)
            first_dir.chmod(0o700)
            second_dir.chmod(0o700)
            first = first_dir / "a"
            second = second_dir / "b"
            first.write_text("a", encoding="utf-8")
            second.write_text("b", encoding="utf-8")
            first.chmod(0o600)
            second.chmod(0o400)

            result = inspect_secret_paths([first, second])

        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["problem_count"], 0)
        self.assertEqual(len(result["files"]), 2)


if __name__ == "__main__":
    unittest.main()
