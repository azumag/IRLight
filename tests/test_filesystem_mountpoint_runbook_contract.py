from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "docs" / "operations" / "filesystem-mountpoint-monitoring.md"
INDEX = ROOT / "docs" / "operations" / "README.md"
SCRIPT = ROOT / "scripts" / "check-filesystem-mountpoint.py"


class FilesystemMountpointRunbookContractTest(unittest.TestCase):
    def test_operations_index_links_runbook_exactly_once(self) -> None:
        index = INDEX.read_text(encoding="utf-8")
        self.assertEqual(
            index.count("](filesystem-mountpoint-monitoring.md)"),
            1,
        )

    def test_runbook_keeps_read_only_fail_closed_contract(self) -> None:
        text = RUNBOOK.read_text(encoding="utf-8")
        for marker in (
            "/proc/self/mountinfo",
            "exact mountpoint",
            "status=CRITICAL reason=mountpoint_missing",
            "status=UNKNOWN reason=mountinfo_unavailable",
            "mount source",
            "volume generation",
            "mount/remount/unmount",
            "presence",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, text)

    def test_script_is_read_only_and_does_not_mutate_mounts_or_files(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("/proc/self/mountinfo", source)
        self.assertIn("os.lstat(path)", source)
        for forbidden in (
            "subprocess",
            "os.mount",
            "os.open(",
            ".write_text(",
            ".touch(",
            ".unlink(",
            ".mkdir(",
            "tempfile",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
