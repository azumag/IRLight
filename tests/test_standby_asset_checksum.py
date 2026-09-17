from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify-standby-asset-checksum.py"

spec = importlib.util.spec_from_file_location("standby_asset_checksum", SCRIPT)
assert spec is not None and spec.loader is not None
MODULE = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = MODULE
spec.loader.exec_module(MODULE)


class StandbyAssetChecksumTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.asset = self.root / "standby.bin"
        self.payload = b"IRLight standby asset\n" * 17
        self.asset.write_bytes(self.payload)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_inspect_returns_canonical_hash_without_path(self) -> None:
        record = MODULE.inspect_asset(self.asset)
        self.assertEqual(
            record,
            {
                "schema_version": 1,
                "algorithm": "sha256",
                "size_bytes": len(self.payload),
                "sha256": hashlib.sha256(self.payload).hexdigest(),
            },
        )
        self.assertNotIn(str(self.asset), json.dumps(record))

    def test_verify_requires_and_checks_expected_metadata(self) -> None:
        digest = hashlib.sha256(self.payload).hexdigest()
        verified = MODULE.verify_asset(
            self.asset,
            expected_sha256=digest,
            expected_size_bytes=len(self.payload),
        )
        self.assertTrue(verified["verified"])

        with self.assertRaisesRegex(MODULE.AssetChecksumError, "sha256"):
            MODULE.verify_asset(self.asset, expected_sha256="0" * 64)
        with self.assertRaisesRegex(MODULE.AssetChecksumError, "size"):
            MODULE.verify_asset(
                self.asset,
                expected_sha256=digest,
                expected_size_bytes=len(self.payload) + 1,
            )
        with self.assertRaisesRegex(MODULE.AssetChecksumError, "requires"):
            MODULE.verify_asset(self.asset, expected_size_bytes=len(self.payload))

    def test_malformed_expected_digest_is_rejected(self) -> None:
        for value in ("A" * 64, "f" * 63, "g" * 64):
            with self.subTest(value=value[:4]):
                with self.assertRaisesRegex(MODULE.AssetChecksumError, "lowercase hex"):
                    MODULE.verify_asset(self.asset, expected_sha256=value)

    def test_symlink_is_rejected(self) -> None:
        alias = self.root / "alias.bin"
        try:
            alias.symlink_to(self.asset)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink unavailable: {exc}")
        with self.assertRaisesRegex(MODULE.AssetChecksumError, "regular file"):
            MODULE.inspect_asset(alias)

    def test_fifo_is_rejected_without_blocking(self) -> None:
        if not hasattr(os, "mkfifo"):
            self.skipTest("FIFO creation unavailable")
        fifo = self.root / "asset.fifo"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(MODULE.AssetChecksumError, "regular file"):
            MODULE.inspect_asset(fifo)

    def test_oversized_sparse_file_is_rejected_before_read(self) -> None:
        oversized = self.root / "oversized.bin"
        with oversized.open("wb") as handle:
            handle.truncate(MODULE.MAX_IMAGE_BYTES + 1)
        with patch.object(MODULE.os, "read", side_effect=AssertionError("must not read")):
            with self.assertRaisesRegex(MODULE.AssetChecksumError, "size"):
                MODULE.inspect_asset(oversized)

    def test_path_replacement_after_open_is_rejected(self) -> None:
        replacement = b"replacement"
        real_open = os.open
        swapped = False

        def swapping_open(path, flags, *args, **kwargs):
            nonlocal swapped
            fd = real_open(path, flags, *args, **kwargs)
            if not swapped and Path(path) == self.asset:
                swapped = True
                self.asset.unlink()
                self.asset.write_bytes(replacement)
            return fd

        with patch.object(MODULE.os, "open", side_effect=swapping_open):
            with self.assertRaisesRegex(MODULE.AssetChecksumError, "path changed"):
                MODULE.inspect_asset(self.asset)
        self.assertTrue(swapped)

    def test_in_place_mutation_during_read_is_rejected(self) -> None:
        source_stat = self.asset.stat()
        identity = (source_stat.st_dev, source_stat.st_ino)
        real_read = os.read
        mutated = False

        def mutating_read(fd: int, size: int) -> bytes:
            nonlocal mutated
            chunk = real_read(fd, size)
            opened = os.fstat(fd)
            if not mutated and (opened.st_dev, opened.st_ino) == identity:
                with self.asset.open("r+b", buffering=0) as handle:
                    handle.seek(0)
                    first = handle.read(1)
                    handle.seek(0)
                    handle.write(bytes([first[0] ^ 1]))
                    os.fsync(handle.fileno())
                mutated = True
            return chunk

        with patch.object(MODULE.os, "read", side_effect=mutating_read):
            with self.assertRaisesRegex(MODULE.AssetChecksumError, "changed"):
                MODULE.inspect_asset(self.asset)
        self.assertTrue(mutated)

    def test_cli_verify_emits_canonical_json_and_does_not_echo_path(self) -> None:
        digest = hashlib.sha256(self.payload).hexdigest()
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "verify",
                str(self.asset),
                "--expected-sha256",
                digest,
                "--expected-size-bytes",
                str(len(self.payload)),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        parsed = json.loads(result.stdout)
        self.assertTrue(parsed["verified"])
        self.assertNotIn(str(self.asset), result.stdout)


if __name__ == "__main__":
    unittest.main()
