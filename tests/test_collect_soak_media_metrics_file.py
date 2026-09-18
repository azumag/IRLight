from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "collect_soak_resource_sample_hardening",
    ROOT / "scripts" / "collect-soak-resource-sample.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

SampleCollectionError = MODULE.SampleCollectionError


def valid_payload() -> dict[str, object]:
    return {
        "bitrate_bps": 3_500_000,
        "av_sync_drift_ms": -4.5,
        "timestamp_errors": 2,
        "unexpected_reconnects": 1,
    }


class _AfterReadAction:
    def __init__(self, handle: object, action: object) -> None:
        self._handle = handle
        self._action = action

    def __enter__(self) -> "_AfterReadAction":
        self._handle.__enter__()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> object:
        return self._handle.__exit__(exc_type, exc, tb)

    def fileno(self) -> int:
        return self._handle.fileno()

    def read(self, size: int = -1) -> bytes:
        data = self._handle.read(size)
        self._action()
        return data


class SoakMediaMetricsFileInputTest(unittest.TestCase):
    def test_regular_file_remains_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "media.json"
            path.write_text(json.dumps(valid_payload()), encoding="utf-8")
            self.assertEqual(
                MODULE.load_media_metrics(path, allow_unmeasured=False),
                {
                    "bitrate_bps": 3_500_000.0,
                    "av_sync_drift_ms": -4.5,
                    "timestamp_errors": 2,
                    "unexpected_reconnects": 1,
                },
            )

    def test_final_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target.json"
            target.write_text(json.dumps(valid_payload()), encoding="utf-8")
            link = root / "media.json"
            link.symlink_to(target)
            with self.assertRaisesRegex(SampleCollectionError, "regular file"):
                MODULE.load_media_metrics(link, allow_unmeasured=False)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO requires POSIX")
    def test_fifo_is_rejected_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "media.fifo"
            os.mkfifo(path)
            with self.assertRaisesRegex(SampleCollectionError, "regular file"):
                MODULE.load_media_metrics(path, allow_unmeasured=False)

    def test_oversized_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "media.json"
            with path.open("wb") as handle:
                handle.truncate(MODULE.MAX_MEDIA_METRICS_BYTES + 1)
            with self.assertRaisesRegex(SampleCollectionError, "exceeds maximum size"):
                MODULE.load_media_metrics(path, allow_unmeasured=False)

    def test_same_inode_mutation_during_read_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "media.json"
            path.write_text(json.dumps(valid_payload()), encoding="utf-8")
            real_open = MODULE._open_media_metrics_readonly

            def wrapped_open(candidate: Path):
                handle = real_open(candidate)

                def mutate() -> None:
                    replacement = valid_payload()
                    replacement["timestamp_errors"] = 12345
                    candidate.write_text(json.dumps(replacement), encoding="utf-8")

                return _AfterReadAction(handle, mutate)

            with mock.patch.object(
                MODULE, "_open_media_metrics_readonly", side_effect=wrapped_open
            ):
                with self.assertRaisesRegex(SampleCollectionError, "changed while reading"):
                    MODULE.load_media_metrics(path, allow_unmeasured=False)

    def test_path_replacement_during_read_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "media.json"
            replacement = root / "replacement.json"
            path.write_text(json.dumps(valid_payload()), encoding="utf-8")
            replacement.write_text(json.dumps(valid_payload()), encoding="utf-8")
            real_open = MODULE._open_media_metrics_readonly

            def wrapped_open(candidate: Path):
                handle = real_open(candidate)
                return _AfterReadAction(handle, lambda: os.replace(replacement, candidate))

            with mock.patch.object(
                MODULE, "_open_media_metrics_readonly", side_effect=wrapped_open
            ):
                with self.assertRaisesRegex(SampleCollectionError, "changed while reading"):
                    MODULE.load_media_metrics(path, allow_unmeasured=False)

    def test_invalid_utf8_is_controlled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "media.json"
            path.write_bytes(b"\xff\xfe")
            with self.assertRaisesRegex(SampleCollectionError, "not valid UTF-8"):
                MODULE.load_media_metrics(path, allow_unmeasured=False)

    def test_recursive_json_failure_is_controlled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "media.json"
            path.write_text(json.dumps(valid_payload()), encoding="utf-8")
            with mock.patch.object(MODULE.json, "loads", side_effect=RecursionError("deep")):
                with self.assertRaisesRegex(SampleCollectionError, "invalid media metrics JSON"):
                    MODULE.load_media_metrics(path, allow_unmeasured=False)

    def test_missing_file_error_does_not_expose_path_or_os_detail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "secret-location" / "media.json"
            with self.assertRaises(SampleCollectionError) as caught:
                MODULE.load_media_metrics(path, allow_unmeasured=False)
            self.assertEqual(str(caught.exception), "cannot inspect media metrics file")
            self.assertNotIn(str(path), str(caught.exception))


if __name__ == "__main__":
    unittest.main()
