from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
EGRESS_DIR = ROOT / "apps" / "egress-gateway"
sys.path.insert(0, str(EGRESS_DIR))

import runtime_secret_file  # noqa: E402
from runtime_secret_file import (  # noqa: E402
    MAX_RUNTIME_SECRET_BYTES,
    RuntimeSecretFileError,
    read_runtime_secret,
)
from secret_inputs import read_destination_url, read_input_uri  # noqa: E402


class EgressRuntimeSecretFileTest(unittest.TestCase):
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

    def test_oversized_file_is_rejected_without_secret_or_path_in_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "operator-private-stream-key-path"
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
            path = Path(directory) / "operator-private-stream-key-path"
            path.write_bytes(b"\xff\xfe")

            with self.assertRaises(RuntimeSecretFileError) as raised:
                read_runtime_secret(path)

            self.assertEqual(
                str(raised.exception),
                "runtime secret file is not valid UTF-8",
            )
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


class EgressSecretInputTest(unittest.TestCase):
    def test_input_file_regular_value_is_used(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input-uri"
            value = "rtsp://user:private-password@example.test/live"
            path.write_text(value + "\n", encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "EGRESS_INPUT_URI_FILE": str(path),
                    "EGRESS_INPUT_URI": "rtsp://must-not-fallback.invalid/live",
                    "IRLIGHT_SECRET_WAIT_SECONDS": "0",
                },
                clear=True,
            ):
                self.assertEqual(read_input_uri(), value)

    def test_input_without_file_preserves_environment_fallback(self) -> None:
        value = "rtsp://fallback.example.test/live"
        with patch.dict(os.environ, {"EGRESS_INPUT_URI": value}, clear=True):
            self.assertEqual(read_input_uri(), value)

    def test_configured_missing_input_file_does_not_fallback_or_leak_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "operator-private-input-location"
            with patch.dict(
                os.environ,
                {
                    "EGRESS_INPUT_URI_FILE": str(path),
                    "EGRESS_INPUT_URI": "rtsp://must-not-fallback.invalid/live",
                    "IRLIGHT_SECRET_WAIT_SECONDS": "0",
                },
                clear=True,
            ):
                with self.assertRaises(RuntimeError) as raised:
                    read_input_uri()

            self.assertEqual(
                str(raised.exception),
                "egress input secret file is unavailable",
            )
            self.assertNotIn(str(path), str(raised.exception))
            self.assertNotIn("must-not-fallback", str(raised.exception))
            self.assertIsNone(raised.exception.__cause__)

    def test_empty_input_file_preserves_invalid_url_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input-uri"
            path.write_text("\n", encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "EGRESS_INPUT_URI_FILE": str(path),
                    "EGRESS_INPUT_URI": "rtsp://must-not-fallback.invalid/live",
                    "IRLIGHT_SECRET_WAIT_SECONDS": "0",
                },
                clear=True,
            ):
                with self.assertRaisesRegex(RuntimeError, "input URL is invalid"):
                    read_input_uri()

    def test_invalid_utf8_input_is_generic_unavailable_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "operator-private-input-location"
            path.write_bytes(b"\xff\xfe")
            with patch.dict(
                os.environ,
                {
                    "EGRESS_INPUT_URI_FILE": str(path),
                    "IRLIGHT_SECRET_WAIT_SECONDS": "0",
                },
                clear=True,
            ):
                with self.assertRaises(RuntimeError) as raised:
                    read_input_uri()

            self.assertEqual(
                str(raised.exception),
                "egress input secret file is unavailable",
            )
            self.assertNotIn(str(path), str(raised.exception))
            self.assertIsNone(raised.exception.__cause__)

    def test_destination_symlink_and_url_validation_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "destination-v2"
            link = root / "destination"
            value = "rtmps://publisher:private-stream-key@example.test/live"
            target.write_text(value + "\n", encoding="utf-8")
            link.symlink_to(target.name)

            self.assertEqual(read_destination_url(link), value)

    def test_destination_reader_redacts_boundary_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "operator-private-destination-location"
            path.write_bytes(b"X" * (MAX_RUNTIME_SECRET_BYTES + 1))

            with self.assertRaises(RuntimeError) as raised:
                read_destination_url(path)

            self.assertEqual(
                str(raised.exception),
                "egress destination secret file is unavailable",
            )
            self.assertNotIn(str(path), str(raised.exception))
            self.assertNotIn("XXXX", str(raised.exception))
            self.assertIsNone(raised.exception.__cause__)

    def test_destination_invalid_url_remains_distinct_from_file_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "destination"
            path.write_text("https://example.test/not-rtmp\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "destination URL is invalid"):
                read_destination_url(path)


class EgressSecretPackagingContractTest(unittest.TestCase):
    def test_production_entrypoint_binds_hardened_readers_before_egress_main(self) -> None:
        source = (EGRESS_DIR / "egress_entrypoint.py").read_text(encoding="utf-8")
        main_start = source.index("def main() -> int:")
        main_source = source[main_start:]

        input_bind = main_source.index("egress._read_input_uri = read_input_uri")
        destination_bind = main_source.index(
            "egress.read_destination_url = _read_destination_url"
        )
        run = main_source.index("return egress.main()")
        self.assertLess(input_bind, run)
        self.assertLess(destination_bind, run)
        self.assertIn("_read_secret_destination_url(path)", source)

    def test_image_packages_secret_boundary_modules(self) -> None:
        dockerfile = (EGRESS_DIR / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("runtime_secret_file.py", dockerfile)
        self.assertIn("secret_inputs.py", dockerfile)
        self.assertIn(
            'CMD ["python3", "-u", "/app/egress_entrypoint.py"]',
            dockerfile,
        )


if __name__ == "__main__":
    unittest.main()
