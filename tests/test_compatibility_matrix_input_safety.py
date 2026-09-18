from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "scripts" / "validate-compatibility-matrix.py"
CANONICAL_MATRIX = ROOT / "docs" / "compatibility-matrix.json"

_spec = importlib.util.spec_from_file_location("compatibility_matrix_validator", VALIDATOR)
assert _spec is not None and _spec.loader is not None
module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(module)


class CompatibilityMatrixInputSafetyTest(unittest.TestCase):
    def test_symlink_matrix_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.json"
            target.write_text("{}\n", encoding="utf-8")
            link = root / "matrix.json"
            link.symlink_to(target)

            with self.assertRaisesRegex(ValueError, "regular file"):
                module.load_matrix(link)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO support is unavailable")
    def test_fifo_matrix_is_rejected_without_opening(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fifo = Path(directory) / "matrix.fifo"
            os.mkfifo(fifo)

            with self.assertRaisesRegex(ValueError, "regular file"):
                module.load_matrix(fifo)

    def test_oversized_matrix_is_rejected_before_parsing(self) -> None:
        fd, name = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        path = Path(name)
        self.addCleanup(path.unlink, missing_ok=True)
        with path.open("wb") as handle:
            handle.truncate(module.MAX_MATRIX_BYTES + 1)

        with self.assertRaisesRegex(ValueError, "exceeds 262144-byte limit"):
            module.load_matrix(path)

    def test_invalid_utf8_is_reported_as_controlled_error(self) -> None:
        fd, name = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        path = Path(name)
        self.addCleanup(path.unlink, missing_ok=True)
        path.write_bytes(b"\xff\xfe\x00")

        with self.assertRaisesRegex(ValueError, "invalid UTF-8"):
            module.load_matrix(path)

    def test_recursive_json_is_reported_as_controlled_error(self) -> None:
        fd, name = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        path = Path(name)
        self.addCleanup(path.unlink, missing_ok=True)
        path.write_text("[" * 5000 + "0" + "]" * 5000, encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "cannot read compatibility matrix"):
            module.load_matrix(path)

    def test_path_replacement_during_read_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "matrix.json"
            replacement = root / "replacement.json"
            payload = CANONICAL_MATRIX.read_text(encoding="utf-8")
            original.write_text(payload, encoding="utf-8")
            replacement.write_text(payload, encoding="utf-8")
            original_stat = os.lstat(original)
            replacement_stat = os.lstat(replacement)
            self.assertNotEqual(
                (original_stat.st_dev, original_stat.st_ino),
                (replacement_stat.st_dev, replacement_stat.st_ino),
            )

            with mock.patch.object(
                module.os,
                "lstat",
                side_effect=[original_stat, replacement_stat],
            ):
                with self.assertRaisesRegex(ValueError, "changed while reading"):
                    module.load_matrix(original)

    def test_in_place_mutation_during_read_is_rejected(self) -> None:
        fd, name = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        path = Path(name)
        self.addCleanup(path.unlink, missing_ok=True)
        path.write_text(CANONICAL_MATRIX.read_text(encoding="utf-8"), encoding="utf-8")
        stable = path.stat()
        mutated = mock.Mock(
            st_mode=stable.st_mode,
            st_dev=stable.st_dev,
            st_ino=stable.st_ino,
            st_size=stable.st_size,
            st_mtime_ns=stable.st_mtime_ns + 1,
            st_ctime_ns=stable.st_ctime_ns + 1,
        )

        with mock.patch.object(
            module.os,
            "fstat",
            side_effect=[stable, stable, mutated],
        ):
            with self.assertRaisesRegex(ValueError, "changed while reading"):
                module.load_matrix(path)


if __name__ == "__main__":
    unittest.main()
