from __future__ import annotations

import multiprocessing
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-api"))

from destination_probe_admission import (  # noqa: E402
    DEFAULT_MAX_CONCURRENT_PROBES,
    DestinationProbeAdmissionBusy,
    DestinationProbeAdmissionConfig,
    DestinationProbeAdmissionUnavailable,
    destination_probe_slot,
)
import catalog_api  # noqa: E402


def _hold_probe_slot(lock_dir: str, ready: object, release: object) -> None:
    config = DestinationProbeAdmissionConfig(
        max_concurrent=1,
        lock_dir=Path(lock_dir),
    )
    with destination_probe_slot(config):
        ready.set()
        release.wait(timeout=10)


class DestinationProbeAdmissionTest(unittest.TestCase):
    def test_nested_probe_is_rejected_and_release_reopens_slot(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-probe-admission-") as root:
            config = DestinationProbeAdmissionConfig(
                max_concurrent=1,
                lock_dir=Path(root) / "locks",
            )
            with destination_probe_slot(config):
                with self.assertRaises(DestinationProbeAdmissionBusy):
                    with destination_probe_slot(config):
                        self.fail("saturated admission unexpectedly granted a slot")

            with destination_probe_slot(config):
                pass

    def test_separate_worker_process_cannot_multiply_limit(self) -> None:
        context = multiprocessing.get_context("fork")
        with tempfile.TemporaryDirectory(prefix="irlight-probe-admission-") as root:
            lock_dir = str(Path(root) / "locks")
            ready = context.Event()
            release = context.Event()
            process = context.Process(
                target=_hold_probe_slot,
                args=(lock_dir, ready, release),
            )
            process.start()
            try:
                self.assertTrue(ready.wait(timeout=5), "child did not acquire probe slot")
                config = DestinationProbeAdmissionConfig(
                    max_concurrent=1,
                    lock_dir=Path(lock_dir),
                )
                with self.assertRaises(DestinationProbeAdmissionBusy):
                    with destination_probe_slot(config):
                        self.fail("second worker unexpectedly acquired the only slot")
            finally:
                release.set()
                process.join(timeout=5)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=5)
            self.assertEqual(process.exitcode, 0)

    def test_symlink_lock_directory_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="irlight-probe-admission-") as root:
            root_path = Path(root)
            target = root_path / "target"
            target.mkdir()
            link = root_path / "locks"
            link.symlink_to(target, target_is_directory=True)
            config = DestinationProbeAdmissionConfig(max_concurrent=1, lock_dir=link)

            with self.assertRaises(DestinationProbeAdmissionUnavailable):
                with destination_probe_slot(config):
                    self.fail("symlink admission directory unexpectedly accepted")

    def test_symlink_slot_file_fails_closed(self) -> None:
        if not hasattr(os, "O_NOFOLLOW"):
            self.skipTest("platform does not expose O_NOFOLLOW")
        with tempfile.TemporaryDirectory(prefix="irlight-probe-admission-") as root:
            lock_dir = Path(root) / "locks"
            lock_dir.mkdir()
            target = Path(root) / "target"
            target.write_text("sentinel", encoding="utf-8")
            (lock_dir / "slot-0.lock").symlink_to(target)
            config = DestinationProbeAdmissionConfig(max_concurrent=1, lock_dir=lock_dir)

            with self.assertRaises(DestinationProbeAdmissionUnavailable):
                with destination_probe_slot(config):
                    self.fail("symlink slot unexpectedly accepted")
            self.assertEqual(target.read_text(encoding="utf-8"), "sentinel")

    def test_invalid_environment_limit_falls_back_to_bounded_default(self) -> None:
        for value in ("0", "33", "not-a-number"):
            with self.subTest(value=value):
                with patch.dict(os.environ, {"IRLIGHT_VERIFY_MAX_CONCURRENT": value}):
                    config = DestinationProbeAdmissionConfig.from_env()
                self.assertEqual(config.max_concurrent, DEFAULT_MAX_CONCURRENT_PROBES)

    def test_busy_api_returns_retryable_503_without_running_store_verify(self) -> None:
        with patch.object(
            catalog_api,
            "destination_probe_slot",
            side_effect=DestinationProbeAdmissionBusy("busy"),
        ), patch.object(catalog_api, "store_verify_destination") as store_verify:
            with self.assertRaises(HTTPException) as caught:
                catalog_api.verify_destination("destination-1", {"id": "user-1"})

        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(caught.exception.detail, "destination verification is busy")
        self.assertEqual(caught.exception.headers, {"Retry-After": "1"})
        store_verify.assert_not_called()

    def test_unavailable_api_fails_closed_without_running_store_verify(self) -> None:
        with patch.object(
            catalog_api,
            "destination_probe_slot",
            side_effect=DestinationProbeAdmissionUnavailable("internal detail"),
        ), patch.object(catalog_api, "store_verify_destination") as store_verify:
            with self.assertRaises(HTTPException) as caught:
                catalog_api.verify_destination("destination-1", {"id": "user-1"})

        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(
            caught.exception.detail,
            "destination verification is unavailable",
        )
        self.assertNotIn("internal detail", str(caught.exception.detail))
        store_verify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
