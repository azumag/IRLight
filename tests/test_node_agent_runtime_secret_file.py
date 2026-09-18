from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "node-agent"))

import runtime_secret_file  # noqa: E402
from agent import _secret_from_file_or_env  # noqa: E402
from runtime_secret_file import (  # noqa: E402
    MAX_RUNTIME_SECRET_BYTES,
    RuntimeSecretFileError,
    read_runtime_secret,
)


class RuntimeSecretFileTest(unittest.TestCase):
    def test_regular_file_and_exact_size_limit_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secret"
            payload = "x" * MAX_RUNTIME_SECRET_BYTES
            path.write_text(payload, encoding="utf-8")

            self.assertEqual(read_runtime_secret(path), payload)

    def test_symlink_to_regular_file_remains_supported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "projected-secret-v2"
            link = root / "secret"
            target.write_text("projected-value\n", encoding="utf-8")
            link.symlink_to(target.name)

            self.assertEqual(read_runtime_secret(link).strip(), "projected-value")

    def test_fifo_is_rejected_before_blocking_read(self) -> None:
        if not hasattr(os, "mkfifo"):
            self.skipTest("FIFO is unavailable on this platform")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secret-fifo"
            os.mkfifo(path)

            with self.assertRaisesRegex(RuntimeSecretFileError, "regular file"):
                read_runtime_secret(path)

    def test_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secret-dir"
            path.mkdir()

            with self.assertRaisesRegex(RuntimeSecretFileError, "regular file"):
                read_runtime_secret(path)

    def test_oversized_file_is_rejected_without_secret_in_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "operator-private-token-path"
            path.write_bytes(b"S" * (MAX_RUNTIME_SECRET_BYTES + 1))

            with self.assertRaises(RuntimeSecretFileError) as raised:
                read_runtime_secret(path)

            message = str(raised.exception)
            self.assertIn("size limit", message)
            self.assertNotIn(str(path), message)
            self.assertNotIn("SSSS", message)
            self.assertIsNone(raised.exception.__cause__)

    def test_invalid_utf8_is_controlled_and_pathless(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "operator-private-token-path"
            path.write_bytes(b"\xff\xfe")

            with self.assertRaises(RuntimeSecretFileError) as raised:
                read_runtime_secret(path)

            self.assertEqual(str(raised.exception), "runtime secret file is not valid UTF-8")
            self.assertNotIn(str(path), str(raised.exception))
            self.assertIsNone(raised.exception.__cause__)

    def test_path_replacement_between_inspection_and_open_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "secret"
            replacement = root / "replacement"
            path.write_text("first-value\n", encoding="utf-8")
            replacement.write_text("second-value\n", encoding="utf-8")
            real_open = runtime_secret_file.os.open
            replaced = False

            def replacing_open(target: object, flags: int) -> int:
                nonlocal replaced
                if not replaced and os.fspath(target) == os.fspath(path):
                    replacement.replace(path)
                    replaced = True
                return real_open(target, flags)

            with patch.object(runtime_secret_file.os, "open", side_effect=replacing_open):
                with self.assertRaisesRegex(RuntimeSecretFileError, "changed during read"):
                    read_runtime_secret(path)

    def test_in_place_mutation_during_read_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secret"
            path.write_text("before!\n", encoding="utf-8")
            real_read = runtime_secret_file.os.read
            mutated = False

            def mutating_read(fd: int, size: int) -> bytes:
                nonlocal mutated
                if not mutated:
                    path.write_text("after!!\n", encoding="utf-8")
                    mutated = True
                return real_read(fd, size)

            with patch.object(runtime_secret_file.os, "read", side_effect=mutating_read):
                with self.assertRaisesRegex(RuntimeSecretFileError, "changed during read"):
                    read_runtime_secret(path)


class AgentSecretFallbackTest(unittest.TestCase):
    def test_empty_file_preserves_existing_environment_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secret"
            path.write_text("\n", encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "IRLIGHT_TEST_SECRET_FILE": str(path),
                    "IRLIGHT_TEST_SECRET": "env-fallback",
                },
                clear=False,
            ):
                self.assertEqual(
                    _secret_from_file_or_env("IRLIGHT_TEST_SECRET"),
                    "env-fallback",
                )

    def test_configured_missing_file_fails_closed_without_path_or_os_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private-bootstrap-token-location"
            with patch.dict(
                os.environ,
                {
                    "IRLIGHT_TEST_SECRET_FILE": str(path),
                    "IRLIGHT_TEST_SECRET": "must-not-fallback",
                },
                clear=False,
            ):
                with self.assertRaises(RuntimeError) as raised:
                    _secret_from_file_or_env("IRLIGHT_TEST_SECRET")

            self.assertEqual(
                str(raised.exception),
                "cannot read IRLIGHT_TEST_SECRET_FILE",
            )
            self.assertNotIn(str(path), str(raised.exception))
            self.assertNotIn("must-not-fallback", str(raised.exception))
            self.assertIsNone(raised.exception.__cause__)


if __name__ == "__main__":
    unittest.main()
