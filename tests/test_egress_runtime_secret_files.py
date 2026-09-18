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

import egress_runtime_secret_file  # noqa: E402
from egress_runtime_secret_file import (  # noqa: E402
    MAX_RUNTIME_SECRET_BYTES,
    RuntimeSecretFileError,
    read_runtime_secret,
)
from secret_inputs import read_destination_url, read_input_uri  # noqa: E402


class EgressRuntimeSecretFileTest(unittest.TestCase):
    def test_regular_file_at_limit_and_projected_symlink_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "projected-secret-v2"
            link = root / "secret"
            payload = "x" * MAX_RUNTIME_SECRET_BYTES
            target.write_text(payload, encoding="utf-8")
            link.symlink_to(target.name)

            self.assertEqual(read_runtime_secret(link), payload)

    def test_fifo_and_directory_are_rejected_before_content_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            directory_path = root / "secret-dir"
            directory_path.mkdir()
            with self.assertRaisesRegex(RuntimeSecretFileError, "regular file"):
                read_runtime_secret(directory_path)

            if hasattr(os, "mkfifo"):
                fifo = root / "secret-fifo"
                os.mkfifo(fifo)
                with self.assertRaisesRegex(RuntimeSecretFileError, "regular file"):
                    read_runtime_secret(fifo)

    def test_oversize_and_invalid_utf8_are_controlled_and_pathless(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            oversized = root / "operator-private-stream-key-path"
            oversized.write_bytes(b"S" * (MAX_RUNTIME_SECRET_BYTES + 1))
            with self.assertRaises(RuntimeSecretFileError) as oversized_error:
                read_runtime_secret(oversized)
            self.assertIn("size limit", str(oversized_error.exception))
            self.assertNotIn(str(oversized), str(oversized_error.exception))
            self.assertIsNone(oversized_error.exception.__cause__)

            invalid = root / "operator-private-invalid-utf8"
            invalid.write_bytes(b"\xff\xfe")
            with self.assertRaises(RuntimeSecretFileError) as invalid_error:
                read_runtime_secret(invalid)
            self.assertEqual(
                str(invalid_error.exception),
                "runtime secret file is not valid UTF-8",
            )
            self.assertNotIn(str(invalid), str(invalid_error.exception))
            self.assertIsNone(invalid_error.exception.__cause__)

    def test_path_replacement_between_inspection_and_open_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "secret"
            replacement = root / "replacement"
            path.write_text("first-value\n", encoding="utf-8")
            replacement.write_text("second-value\n", encoding="utf-8")
            real_open = egress_runtime_secret_file.os.open
            replaced = False

            def replacing_open(target: object, flags: int) -> int:
                nonlocal replaced
                if not replaced and os.fspath(target) == os.fspath(path):
                    replacement.replace(path)
                    replaced = True
                return real_open(target, flags)

            with patch.object(
                egress_runtime_secret_file.os,
                "open",
                side_effect=replacing_open,
            ):
                with self.assertRaisesRegex(RuntimeSecretFileError, "changed during read"):
                    read_runtime_secret(path)

    def test_in_place_mutation_during_read_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secret"
            path.write_text("before!\n", encoding="utf-8")
            real_read = egress_runtime_secret_file.os.read
            mutated = False

            def mutating_read(fd: int, size: int) -> bytes:
                nonlocal mutated
                if not mutated:
                    path.write_text("after!!\n", encoding="utf-8")
                    mutated = True
                return real_read(fd, size)

            with patch.object(
                egress_runtime_secret_file.os,
                "read",
                side_effect=mutating_read,
            ):
                with self.assertRaisesRegex(RuntimeSecretFileError, "changed during read"):
                    read_runtime_secret(path)


class EgressSecretInputTest(unittest.TestCase):
    def test_input_file_value_and_no_file_environment_fallback_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input-uri"
            file_value = "rtsp://user:private-password@example.test/live"
            path.write_text(file_value + "\n", encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "EGRESS_INPUT_URI_FILE": str(path),
                    "EGRESS_INPUT_URI": "rtsp://must-not-fallback.invalid/live",
                    "IRLIGHT_SECRET_WAIT_SECONDS": "0",
                },
                clear=True,
            ):
                self.assertEqual(read_input_uri(), file_value)

        fallback = "rtsp://fallback.example.test/live"
        with patch.dict(os.environ, {"EGRESS_INPUT_URI": fallback}, clear=True):
            self.assertEqual(read_input_uri(), fallback)

    def test_configured_missing_or_invalid_utf8_input_fails_without_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = root / "operator-private-input-location"
            for path in (missing, root / "invalid-utf8"):
                if path.name == "invalid-utf8":
                    path.write_bytes(b"\xff\xfe")
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

    def test_empty_input_file_remains_invalid_instead_of_env_fallback(self) -> None:
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

    def test_destination_projected_symlink_and_validation_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "destination-v2"
            link = root / "destination"
            value = "rtmps://publisher:private-stream-key@example.test/live"
            target.write_text(value + "\n", encoding="utf-8")
            link.symlink_to(target.name)
            self.assertEqual(read_destination_url(link), value)

            target.write_text("https://example.test/not-rtmp\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "destination URL is invalid"):
                read_destination_url(link)

    def test_destination_boundary_failure_is_generic_and_redacted(self) -> None:
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


class EgressSecretPackagingContractTest(unittest.TestCase):
    def test_direct_egress_uses_hardened_secret_readers(self) -> None:
        source = (EGRESS_DIR / "egress.py").read_text(encoding="utf-8")
        self.assertIn(
            "from secret_inputs import read_destination_url, read_input_uri as _read_input_uri",
            source,
        )
        self.assertNotIn("def _read_input_uri()", source)
        self.assertNotIn("def read_destination_url(path: Path)", source)

    def test_production_entrypoint_binds_hardened_readers_before_egress_main(self) -> None:
        source = (EGRESS_DIR / "egress_entrypoint.py").read_text(encoding="utf-8")
        main_source = source[source.index("def main() -> int:") :]
        run = main_source.index("return egress.main()")
        self.assertLess(
            main_source.index("egress._read_input_uri = read_input_uri"),
            run,
        )
        self.assertLess(
            main_source.index("egress.read_destination_url = _read_destination_url"),
            run,
        )
        self.assertIn("_read_secret_destination_url(path)", source)

    def test_image_packages_service_unique_secret_boundary_modules(self) -> None:
        dockerfile = (EGRESS_DIR / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("egress_runtime_secret_file.py", dockerfile)
        self.assertIn("secret_inputs.py", dockerfile)
        self.assertNotIn(" runtime_secret_file.py", dockerfile)
        self.assertIn(
            'CMD ["python3", "-u", "/app/egress_entrypoint.py"]',
            dockerfile,
        )


if __name__ == "__main__":
    unittest.main()
