from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

import node_internal  # noqa: E402
from node_internal import (  # noqa: E402
    MAX_ADMIN_TOKEN_FILE_BYTES,
    configured_admin_token_digests,
    hash_token,
)


class ControlApiAdminTokenFileTest(unittest.TestCase):
    def _configured(self, path: Path, *, env_tokens: str = ""):
        return patch.dict(
            os.environ,
            {
                "NODE_INTERNAL_ADMIN_TOKEN_FILE": str(path),
                "NODE_INTERNAL_ADMIN_TOKENS": env_tokens,
            },
            clear=False,
        )

    def _assert_unavailable(self, path: Path, *, env_tokens: str = "must-not-fallback") -> HTTPException:
        with self._configured(path, env_tokens=env_tokens):
            with self.assertRaises(HTTPException) as raised:
                configured_admin_token_digests()

        error = raised.exception
        self.assertEqual(error.status_code, 503)
        self.assertEqual(error.detail, "node admin authentication is unavailable")
        self.assertNotIn(str(path), str(error.detail))
        self.assertNotIn(env_tokens, str(error.detail))
        self.assertIsNone(error.__cause__)
        return error

    def test_regular_file_and_environment_tokens_are_both_honored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "admin-token"
            path.write_text("file-token\n", encoding="utf-8")

            with self._configured(path, env_tokens="env-token"):
                self.assertEqual(
                    configured_admin_token_digests(),
                    {hash_token("file-token"), hash_token("env-token")},
                )

    def test_exact_size_limit_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "admin-token"
            value = "x" * MAX_ADMIN_TOKEN_FILE_BYTES
            path.write_text(value, encoding="utf-8")

            with self._configured(path):
                self.assertEqual(
                    configured_admin_token_digests(),
                    {hash_token(value)},
                )

    def test_empty_file_preserves_existing_environment_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "admin-token"
            path.write_text("\n", encoding="utf-8")

            with self._configured(path, env_tokens="env-token"):
                self.assertEqual(
                    configured_admin_token_digests(),
                    {hash_token("env-token")},
                )

    def test_symlink_to_regular_file_remains_supported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "projected-secret-v2"
            link = root / "admin-token"
            target.write_text("projected-token\n", encoding="utf-8")
            link.symlink_to(target.name)

            with self._configured(link):
                self.assertEqual(
                    configured_admin_token_digests(),
                    {hash_token("projected-token")},
                )

    def test_missing_configured_file_does_not_fallback_to_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private-admin-token-location"
            self._assert_unavailable(path)

    def test_fifo_is_rejected_without_blocking(self) -> None:
        if not hasattr(os, "mkfifo"):
            self.skipTest("FIFO is unavailable on this platform")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "admin-token-fifo"
            os.mkfifo(path)
            self._assert_unavailable(path)

    def test_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "admin-token-dir"
            path.mkdir()
            self._assert_unavailable(path)

    def test_oversized_file_is_rejected_without_path_or_value_exposure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "operator-private-admin-token"
            path.write_bytes(b"S" * (MAX_ADMIN_TOKEN_FILE_BYTES + 1))
            self._assert_unavailable(path)

    def test_invalid_utf8_is_rejected_with_fixed_public_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "operator-private-admin-token"
            path.write_bytes(b"\xff\xfe")
            self._assert_unavailable(path)

    def test_path_replacement_between_inspection_and_open_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "admin-token"
            replacement = root / "replacement"
            path.write_text("first-token\n", encoding="utf-8")
            replacement.write_text("second-token\n", encoding="utf-8")
            real_open = node_internal.os.open
            replaced = False

            def replacing_open(target: object, flags: int) -> int:
                nonlocal replaced
                if not replaced and os.fspath(target) == os.fspath(path):
                    replacement.replace(path)
                    replaced = True
                return real_open(target, flags)

            with patch.object(node_internal.os, "open", side_effect=replacing_open):
                self._assert_unavailable(path)

    def test_in_place_mutation_during_read_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "admin-token"
            path.write_text("before!\n", encoding="utf-8")
            real_read = node_internal.os.read
            mutated = False

            def mutating_read(fd: int, size: int) -> bytes:
                nonlocal mutated
                if not mutated:
                    path.write_text("after!!\n", encoding="utf-8")
                    mutated = True
                return real_read(fd, size)

            with patch.object(node_internal.os, "read", side_effect=mutating_read):
                self._assert_unavailable(path)


if __name__ == "__main__":
    unittest.main()
